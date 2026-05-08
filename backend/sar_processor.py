"""
Sentinel-1 SAR processing pipeline (Phase 4.3 of docs/roadmap.md).

Reads a downloaded scene from R2, runs the CFAR detector tile-by-tile,
geocodes detections via the GCPs in the SAFE annotation XML, and
persists results to sar_detections.

Why this lives separate from sar.py / cfar.py:
  - sar.py    = catalog discovery + download orchestration + Copernicus auth
  - cfar.py   = pure-numpy CFAR algorithm (no I/O, easy to unit-test)
  - this     = bridge between them: reads SAFE.zip from R2, applies CFAR
               to the VV-polarization GRDH amplitude, persists detections.

Design choices:
  - Read VV (HH for HH-only modes, but we use IW which is dual-pol VV+VH).
    VV is the standard channel for vessel detection — VH brings noise
    suppression but adds complexity; Phase 4.x can pick that up.
  - Open via /vsizip+/vsis3/ so the .SAFE.zip never lands on local disk.
    GDAL handles HTTP range reads through R2's S3-compatible API.
  - Process in 4096×4096 tiles to keep peak memory bounded (~250 MB per
    tile float32 + integral images). Fits comfortably under 1 GB Fly VM.
  - Geocode via the geolocationGridPoint elements in the SAFE annotation
    XML (210 GCPs on a regular grid for IW GRDH). Use bilinear
    interpolation rather than the GeoTIFF's CRS — the COG TIFFs Copernicus
    ships have crs=None and pixel-space bounds.
  - Edge-buffer: skip detections within `EDGE_GUARD_PX` of the tile edge
    to avoid CFAR boundary artefacts where the reflection-padded clutter
    estimate is biased.

Memory at runtime:
  - 4096×4096 uint16 tile read:           32 MB
  - float32 conversion:                   64 MB
  - Two integral images (mu + threshold): 128 MB
  - Total peak per tile:                  ~225 MB
  - Plus rasterio's GDAL block cache (64 MB cap by env)
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import numpy as np

from cfar import CfarConfig, detect_vessels
import fixed_structures
import land_mask

log = logging.getLogger("sar_processor")


# ---------------------------------------------------------------------- knobs

# Tile size — bigger tiles waste memory; smaller tiles add HTTP round-trips.
# Sized to fit per-tile peak memory + the live AIS engine state under 1 GB.
# CFAR's integral-image arrays scale O(tile_px^2 * 8 bytes), so:
#   4096 tile → ~500 MB peak per tile (OOM on 1 GB Fly VM)
#   2048 tile → ~130 MB peak per tile (OOM observed on 2026-05-08 with
#              live AIS + audit archive coexisting — total anon-rss 848 MB)
#   1024 tile → ~35 MB peak per tile (fits comfortably; ~4x more tiles
#              but each is ~4x faster, so net wall clock ≈ unchanged)
# At 10 m/pixel IW GRDH, each 1024 tile covers ~10 km × 10 km — still
# plenty of context for the CFAR clutter estimate (training annulus is
# only ~140 m).
TILE_PX = 1024

# Buffer to drop near tile edges (CFAR's reflect padding biases the clutter
# estimate within ~train+guard pixels of the boundary).
EDGE_GUARD_PX = 100

# Sentinel-1 IW GRDH ground-range pixel spacing.
S1_GRDH_PIXEL_M = 10.0

# Texas-shoreline AOI per memory/semper_safe_aoi.md. Re-filter detections
# against this so we don't persist hits in adjacent scenes that drift north.
AOI_LAT_MIN, AOI_LAT_MAX = 25.5, 30.5
AOI_LON_MIN, AOI_LON_MAX = -98.0, -93.5

# CFAR PFA — 1e-7 over the tile gives a few false alarms per scene that
# the cluster filters then knock down to plausible vessel candidates.
DEFAULT_PFA = 1e-7

# Suppression radius for known offshore platforms. Detections within this
# distance of a structure in fixed_structures.gulf_offshore_platforms.json
# are silently dropped during ingest. 200 m comfortably exceeds the GCP
# geocoder residual (~30 m) plus the IW GRDH pixel footprint (~10 m),
# while staying tight enough that a vessel transiting near a platform
# isn't suppressed.
FIXED_STRUCTURE_RADIUS_M = 200.0

# Minimum VV/VH amplitude ratio (dB) for a detection to be kept. Sentinel-1
# IW GRDH dual-pol scenes (1SDV) ship both bands. Real metallic vessels
# return strongly in VV (co-pol) and weakly in VH (cross-pol) → ratio
# typically 6-12 dB. Biological clutter (slicks, breaking waves, Bragg
# roughness) returns similarly in both → ratio 0-3 dB. Setting the floor
# at 4 dB drops the bulk of weather-induced false positives while keeping
# real ship returns. Tunable per-AOI; can be relaxed for fishing fleets
# (small wooden hulls have lower VV co-pol response).
VV_VH_MIN_RATIO_DB = 4.0


# ---------------------------------------------------------------------- helpers


def _r2_presigned_url(scene_id: str, expires_in: int = 3600) -> str:
    """Generate a presigned GET URL for the scene zip in R2.

    We only use this to read the SAFE annotation XML (~1.7 MB) via raw
    HTTP range requests. The TIFF read goes through GDAL /vsis3/ which
    uses the same R2 endpoint via env-var-based credentials — unsigned.
    """
    from db import archive  # reuse the same R2 config validator

    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        raise RuntimeError("R2 not configured")
    client = archive._r2_client(cfg)  # noqa: SLF001
    key = f"sar/scenes/{scene_id}.SAFE.zip"
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg["bucket"], "Key": key},
        ExpiresIn=expires_in,
    )


def _scene_zip_size(scene_id: str) -> int:
    """Get the .SAFE.zip ContentLength in R2 — needed to range-read the EOCD."""
    from db import archive

    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        raise RuntimeError("R2 not configured")
    client = archive._r2_client(cfg)  # noqa: SLF001
    key = f"sar/scenes/{scene_id}.SAFE.zip"
    h = client.head_object(Bucket=cfg["bucket"], Key=key)
    return int(h["ContentLength"])


def _read_zip_entry(url: str, total_size: int, name_substr: str,
                    name_endswith: str, exclude: str | None = None) -> tuple[str, bytes]:
    """Read a STORED-method entry out of a remote .zip via HTTP range reads.

    Sentinel-1 SAFE .zip uses STORED (uncompressed) for all internal
    files so we can compute the data byte range from the central-directory
    record + local file header without ever decompressing.

    Returns (entry_name, raw_bytes). Raises if not found.
    """
    import struct

    # 1) Read trailer (last 64 KB) to find the End-of-Central-Directory record.
    tail = urlopen(
        Request(url, headers={"Range": f"bytes={max(0, total_size - 65536)}-{total_size - 1}"}),
        timeout=30,
    ).read()
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise RuntimeError("EOCD record not found in .zip trailer")
    cd_size = struct.unpack("<I", tail[i + 12:i + 16])[0]
    cd_off = struct.unpack("<I", tail[i + 16:i + 20])[0]

    # 2) Pull the central directory and walk it.
    cd = urlopen(
        Request(url, headers={"Range": f"bytes={cd_off}-{cd_off + cd_size - 1}"}),
        timeout=30,
    ).read()

    p = 0
    found = None
    while p < len(cd):
        if cd[p:p + 4] != b"PK\x01\x02":
            break
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[p + 28:p + 34])
        method = struct.unpack("<H", cd[p + 10:p + 12])[0]
        csize = struct.unpack("<I", cd[p + 20:p + 24])[0]
        local_off = struct.unpack("<I", cd[p + 42:p + 46])[0]
        name_b = cd[p + 46:p + 46 + name_len]
        name = name_b.decode("utf-8", errors="replace")
        ok = (name_substr in name) and name.endswith(name_endswith)
        if ok and (exclude is None or exclude not in name):
            found = (name, method, csize, local_off)
            break
        p += 46 + name_len + extra_len + comment_len

    if found is None:
        raise RuntimeError(f"entry not found: substr={name_substr!r} ends={name_endswith!r}")

    name, method, csize, local_off = found

    # 3) Read the local file header (variable size) to find the data offset.
    lh = urlopen(
        Request(url, headers={"Range": f"bytes={local_off}-{local_off + 30 + 4096}"}),
        timeout=30,
    ).read()
    if lh[:4] != b"PK\x03\x04":
        raise RuntimeError(f"local header sig mismatch at {local_off}")
    lh_name_len, lh_extra_len = struct.unpack("<HH", lh[26:30])
    data_off = local_off + 30 + lh_name_len + lh_extra_len
    data_end = data_off + csize - 1

    # 4) Pull the entry data.
    body = urlopen(
        Request(url, headers={"Range": f"bytes={data_off}-{data_end}"}),
        timeout=120,
    ).read()
    if method == 8:  # DEFLATE — annotation XMLs are usually STORED but tolerate it
        import zlib
        body = zlib.decompress(body, -15)
    elif method != 0:
        raise RuntimeError(f"unsupported zip compression method {method}")

    return name, body


def _build_geocoder(annotation_xml: bytes):
    """Parse the annotation XML's geolocationGridPoints into bilinear interpolators.

    Returns a function (line, pixel) → (lat, lon). Sentinel-1 IW GRDH ships
    GCPs on a regular grid (10 lines × 21 pixels = 210 points by default).
    """
    from scipy.interpolate import RegularGridInterpolator

    root = ET.fromstring(annotation_xml)
    gcps = []
    for gp in root.iter("geolocationGridPoint"):
        gcps.append((
            int(gp.findtext("line")),
            int(gp.findtext("pixel")),
            float(gp.findtext("latitude")),
            float(gp.findtext("longitude")),
        ))
    if not gcps:
        raise RuntimeError("no geolocationGridPoint elements in annotation XML")

    ulines = sorted({g[0] for g in gcps})
    upixels = sorted({g[1] for g in gcps})
    lat_grid = np.zeros((len(ulines), len(upixels)))
    lon_grid = np.zeros((len(ulines), len(upixels)))
    line_idx = {l: i for i, l in enumerate(ulines)}
    pix_idx = {p: i for i, p in enumerate(upixels)}
    for line, pix, la, lo in gcps:
        lat_grid[line_idx[line], pix_idx[pix]] = la
        lon_grid[line_idx[line], pix_idx[pix]] = lo
    lat_i = RegularGridInterpolator((ulines, upixels), lat_grid,
                                    bounds_error=False, fill_value=None)
    lon_i = RegularGridInterpolator((ulines, upixels), lon_grid,
                                    bounds_error=False, fill_value=None)

    def to_latlon(line: float, pixel: float) -> tuple[float, float]:
        return float(lat_i([[line, pixel]])[0]), float(lon_i([[line, pixel]])[0])

    return to_latlon, len(gcps)


def _gen_detection_id() -> str:
    return f"sard_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------- main


def process_scene(scene_id: str, *, pfa: float = DEFAULT_PFA,
                  tile_px: int = TILE_PX, fuse_engine=None) -> dict:
    """End-to-end detection pipeline for one downloaded SAR scene.

    Steps:
      1. Verify sar_scenes row state == 'downloaded'
      2. Generate R2 presigned URL + read annotation XML for GCPs
      3. Open the scene via /vsizip+/vsis3/ in rasterio
      4. Iterate tiles, run CFAR per tile, geocode + filter to AOI
      5. Bulk-insert into sar_detections
      6. Update sar_scenes.state = 'detected'

    Returns a summary dict for the audit log + admin response.
    """
    import rasterio
    from rasterio.windows import Window

    # Lazy DB imports so importing sar_processor.py doesn't pull the DB stack
    # — useful for unit tests that mock these paths.
    from db import archive
    from db import models as dbm
    from db.session import session_scope
    from geoalchemy2.shape import from_shape
    from shapely.geometry import Point

    # 1) Look up scene + bail if not ready.
    with session_scope() as s:
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is None:
            raise RuntimeError(f"sar_scenes row not found: {scene_id}")
        if scene.state != "downloaded":
            return {"scene_id": scene_id,
                    "skipped": f"state={scene.state} (need 'downloaded')",
                    "raw_url": scene.raw_url}
        attrs = dict(scene.attrs or {})
        scene_name = attrs.get("name", "")

    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        raise RuntimeError("R2 not configured")

    # 2) Annotation XML → GCPs → geocoder
    log.info("[%s] reading annotation XML from R2", scene_id)
    url = _r2_presigned_url(scene_id)
    size = _scene_zip_size(scene_id)
    name, ann_bytes = _read_zip_entry(
        url, size,
        name_substr="/annotation/s1a-iw-grd-vv-",
        name_endswith=".xml",
        exclude="/calibration/",
    )
    if "/rfi/" in name:
        # Defensive — should not match given the substr filter
        raise RuntimeError(f"matched RFI XML by mistake: {name}")
    to_latlon, n_gcps = _build_geocoder(ann_bytes)
    log.info("[%s] geocoder ready (%d GCPs)", scene_id, n_gcps)

    # 3) Set up GDAL env so /vsis3/ talks to R2 via path-style addressing.
    os.environ.update({
        "AWS_ACCESS_KEY_ID":     cfg["access_key_id"],
        "AWS_SECRET_ACCESS_KEY": cfg["secret_access_key"],
        # R2 endpoint hostname (no scheme) — GDAL prepends https.
        "AWS_S3_ENDPOINT":       f"{cfg['account_id']}.r2.cloudflarestorage.com",
        "AWS_VIRTUAL_HOSTING":   "FALSE",
        "AWS_HTTPS":             "YES",
        "AWS_REGION":            "auto",
        "VSI_CACHE":             "TRUE",
        "VSI_CACHE_SIZE":        str(64 << 20),
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tiff,.tif,.zip",
    })

    # The TIFF entry name is parallel to the XML's, just under measurement/.
    # e.g. /annotation/s1a-iw-grd-vv-20260507t...001-cog.xml
    #   →  /measurement/s1a-iw-grd-vv-20260507t...001-cog.tiff
    inner_tiff = name.replace("/annotation/", "/measurement/").rsplit(".", 1)[0] + ".tiff"
    gdal_path = (f"/vsizip//vsis3/{cfg['bucket']}/sar/scenes/"
                 f"{scene_id}.SAFE.zip/{inner_tiff}")
    # VH measurement TIFF for multi-pol discrimination. The product is
    # 1SDV (dual-pol VV+VH) for our IW GRDH scenes — the VH band sits
    # next to VV with the same naming except the polarization marker.
    # Also bump the suffix index from -001 to -002 (Copernicus convention).
    inner_tiff_vh = (
        inner_tiff
        .replace("-vv-", "-vh-")
        .replace("-001-", "-002-")
    )
    gdal_path_vh = (f"/vsizip//vsis3/{cfg['bucket']}/sar/scenes/"
                    f"{scene_id}.SAFE.zip/{inner_tiff_vh}")

    # 4) Tile + CFAR
    cfg_cfar = CfarConfig(pfa=pfa)
    detections: list[dict] = []
    t0 = time.time()
    n_tiles = 0
    n_raw = 0
    n_platform_dropped = 0     # detections suppressed via fixed_structures
    n_land_dropped = 0         # detections suppressed via land_mask
    n_vhratio_dropped = 0      # detections suppressed via low VV/VH (clutter)
    log.info("[%s] platform suppression: %d known structures, radius %.0f m",
             scene_id, fixed_structures.platform_count(),
             FIXED_STRUCTURE_RADIUS_M)
    log.info("[%s] land mask: %s", scene_id,
             "enabled" if land_mask.is_loaded() else "disabled (file missing)")

    # Open VH alongside VV. If VH is unavailable (single-pol HH product
    # or download anomaly) we degrade gracefully — VV-only behavior with
    # vv_vh_ratio_db left null on each detection.
    vh_src = None
    try:
        vh_src = rasterio.open(gdal_path_vh)
        log.info("[%s] multi-pol VV+VH discrimination enabled", scene_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] VH band unavailable (%s) — VV-only mode",
                    scene_id, exc)

    log.info("[%s] opening %s", scene_id, inner_tiff)
    with rasterio.open(gdal_path) as src:
        H, W = src.height, src.width
        log.info("[%s] scene %dx%d, tiling at %d", scene_id, W, H, tile_px)

        for r0 in range(0, H, tile_px):
            for c0 in range(0, W, tile_px):
                r1 = min(r0 + tile_px, H)
                c1 = min(c0 + tile_px, W)
                if (r1 - r0) < 256 or (c1 - c0) < 256:
                    continue
                window = Window(c0, r0, c1 - c0, r1 - r0)
                arr = src.read(1, window=window)
                # No-data tiles (over-land outside swath) — skip.
                if float(arr.mean()) < 5.0:
                    continue
                n_tiles += 1
                tile_dets = detect_vessels(arr, cfg=cfg_cfar)
                n_raw += len(tile_dets)
                # Read the matching VH window once per tile so we can
                # sample at every detection centroid without re-reading.
                # If VH is unavailable, vh_arr stays None — ratio falls
                # through to null and we skip the discrimination filter.
                vh_arr = None
                if vh_src is not None and tile_dets:
                    try:
                        vh_arr = vh_src.read(1, window=window)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[%s] VH read failed at %s (%s) — "
                                    "tile in VV-only", scene_id, window, exc)
                for d in tile_dets:
                    if (d.centroid_row < EDGE_GUARD_PX or
                            d.centroid_col < EDGE_GUARD_PX or
                            (arr.shape[0] - d.centroid_row) < EDGE_GUARD_PX or
                            (arr.shape[1] - d.centroid_col) < EDGE_GUARD_PX):
                        continue
                    sr = r0 + d.centroid_row
                    sc = c0 + d.centroid_col
                    lat, lon = to_latlon(sr, sc)
                    if not (AOI_LAT_MIN <= lat <= AOI_LAT_MAX
                            and AOI_LON_MIN <= lon <= AOI_LON_MAX):
                        continue
                    # Drop returns over dry land. Sentinel-1 IW GRDH covers
                    # ~250×200 km, so most scenes span continent + ocean.
                    # CFAR over land flags buildings / ag patterns / etc as
                    # vessels — see land_mask.py docstring.
                    if land_mask.is_on_land(lat, lon):
                        n_land_dropped += 1
                        continue
                    # Drop returns coincident with a known fixed structure
                    # (oil rig, production platform, light vessel, etc).
                    # See fixed_structures.py docstring for sourcing notes.
                    if fixed_structures.is_near_fixed_structure(
                            lat, lon, radius_m=FIXED_STRUCTURE_RADIUS_M):
                        n_platform_dropped += 1
                        continue
                    # VV/VH discrimination — sample VH amplitude in a 5×5
                    # window around the centroid (matches the typical
                    # cluster footprint at IW GRDH 10-m pixel spacing).
                    # Vessels: VV ≫ VH (high ratio). Biological clutter
                    # / wind-driven Bragg roughness: similar VV+VH (low
                    # ratio). Drop candidates with ratio_db below the
                    # threshold to suppress false alarms over rough seas.
                    vv_vh_ratio_db: float | None = None
                    if vh_arr is not None:
                        rr = int(d.centroid_row)
                        cc = int(d.centroid_col)
                        h_h = vh_arr.shape[0]
                        h_w = vh_arr.shape[1]
                        ws = 2  # half-width — 5×5 window
                        r_lo = max(0, rr - ws); r_hi = min(h_h, rr + ws + 1)
                        c_lo = max(0, cc - ws); c_hi = min(h_w, cc + ws + 1)
                        vh_patch = vh_arr[r_lo:r_hi, c_lo:c_hi]
                        vv_patch = arr[r_lo:r_hi, c_lo:c_hi]
                        if vh_patch.size:
                            # GRDH amplitude → intensity = amp²; ratio_dB
                            # = 10*log10(VV²/VH²) = 20*log10(VV/VH).
                            # Use mean to suppress single-pixel outliers.
                            vv_mean = float(vv_patch.astype("float64").mean())
                            vh_mean = float(vh_patch.astype("float64").mean())
                            if vh_mean > 1.0:    # avoid log(near-zero)
                                vv_vh_ratio_db = 20.0 * math.log10(
                                    max(vv_mean, 1.0) / vh_mean
                                )
                                if vv_vh_ratio_db < VV_VH_MIN_RATIO_DB:
                                    n_vhratio_dropped += 1
                                    continue
                    # Confidence heuristic: scale rcs_db with a squashing function.
                    # rcs 60 dB → 0.5; 80 dB → 0.85; 100 dB → 0.95.
                    conf = 1.0 / (1.0 + math.exp(-(d.rcs_db - 70.0) / 8.0))
                    detections.append({
                        "detection_id": _gen_detection_id(),
                        "scene_id": scene_id,
                        "lat": lat, "lon": lon,
                        "rcs_db": float(d.rcs_db),
                        "length_m": float(d.length_px) * S1_GRDH_PIXEL_M,
                        "confidence": float(conf),
                        "vv_vh_ratio_db": vv_vh_ratio_db,
                        "scene_row": int(sr), "scene_col": int(sc),
                        "n_pixels": int(d.n_pixels),
                    })

    if vh_src is not None:
        try:
            vh_src.close()
        except Exception:  # noqa: BLE001
            pass
    elapsed = time.time() - t0
    log.info("[%s] CFAR done in %.1fs — %d tiles, %d raw, %d kept, "
             "%d on land, %d on fixed structures, %d low VV/VH ratio",
             scene_id, elapsed, n_tiles, n_raw, len(detections),
             n_land_dropped, n_platform_dropped, n_vhratio_dropped)

    # 5) Persist
    detected_at = datetime.now(timezone.utc)
    with session_scope() as s:
        for det in detections:
            row = dbm.SarDetectionRow(
                detection_id=det["detection_id"],
                scene_id=scene_id,
                geom=from_shape(Point(det["lon"], det["lat"]), srid=4326),
                detected_at=detected_at,
                rcs_db=det["rcs_db"],
                length_m=det["length_m"],
                confidence=det["confidence"],
                vv_vh_ratio_db=det.get("vv_vh_ratio_db"),
                matched_entity_id=None,
            )
            s.add(row)
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is not None:
            scene.state = "detected"
            scene.attrs = {
                **(scene.attrs or {}),
                "detection_summary": {
                    "n_tiles": n_tiles,
                    "n_raw": n_raw,
                    "n_kept": len(detections),
                    "n_land_dropped": n_land_dropped,
                    "n_platform_dropped": n_platform_dropped,
                    "n_vhratio_dropped": n_vhratio_dropped,
                    "vh_enabled": vh_src is not None,
                    "vv_vh_min_ratio_db": VV_VH_MIN_RATIO_DB,
                    "elapsed_s": round(elapsed, 1),
                    "pfa": pfa,
                    "tile_px": tile_px,
                    "detected_at": detected_at.isoformat(),
                },
            }

    fusion_summary = None
    if fuse_engine is not None and detections:
        try:
            fusion_summary = fuse_detections(fuse_engine, scene_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] fusion step failed (detections persisted): %s",
                          scene_id, exc)

    return {
        "scene_id": scene_id,
        "scene_name": scene_name,
        "n_tiles": n_tiles,
        "n_raw": n_raw,
        "n_detections": len(detections),
        "elapsed_s": round(elapsed, 1),
        "pfa": pfa,
        "fusion": fusion_summary,
    }


# ---------------------------------------------------------------------- fusion


def fuse_detections(engine, scene_id: str) -> dict:
    """Match each sar_detection to AIS vessels (or classify as dark).

    Hands each detection to engine.ingest as a SourceType.SAR observation.
    The engine's _ingest_sar already does the matching:
      - VESSEL / AIS_GAP within fusion window → match, append to entity
      - existing DARK_VESSEL nearby → track continuity (still no AIS match)
      - none of the above → fresh DARK_VESSEL entity

    We then set sar_detections.matched_entity_id ONLY when the resulting
    entity has an AIS lineage (type ∈ {VESSEL, AIS_GAP}). Dark vessels
    (new or continuing) leave matched_entity_id null because the
    /detections endpoint and the frontend layer use that null vs set
    distinction to color points red (dark) vs green (matched).

    Idempotency: skips detections that already have matched_entity_id set.
    Re-running fuse on the same scene is safe and only fills in unmatched
    rows. Note that the engine itself is NOT idempotent — re-feeding the
    same observation twice would inflate observation_ids.

    Returns counts dict for audit + admin response.
    """
    from db import models as dbm
    from db.session import session_scope
    from sqlalchemy import select as sa_select
    from geoalchemy2.shape import to_shape

    from models import EntityType, Observation, SourceType

    # Load scene + unmatched detections in one txn.
    with session_scope() as s:
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is None:
            raise RuntimeError(f"sar_scenes row not found: {scene_id}")
        rows = s.execute(
            sa_select(dbm.SarDetectionRow)
            .where(dbm.SarDetectionRow.scene_id == scene_id)
            .where(dbm.SarDetectionRow.matched_entity_id.is_(None))
        ).scalars().all()
        # Snapshot the data we need outside the session — the rows objects
        # detach when the session closes.
        scene_t = scene.acquired_at
        det_payload = []
        for r in rows:
            pt = to_shape(r.geom)
            det_payload.append({
                "detection_id": r.detection_id,
                "lon": pt.x, "lat": pt.y,
                "rcs_db": r.rcs_db, "length_m": r.length_m,
                "confidence": r.confidence,
            })

    log.info("[%s] fusing %d unmatched detections at scene t=%s",
             scene_id, len(det_payload), scene_t.isoformat())

    n_ais_matched = 0
    n_dark_continued = 0
    n_dark_new = 0
    matched: dict[str, str] = {}    # detection_id → entity_id

    import h3
    from models import Geom
    H3_RES = 8

    for d in det_payload:
        # Pre-snapshot the engine's known dark vessels so we can tell
        # afterward whether the engine matched to an existing one
        # (track continuity) vs created a new one.
        existing_dark_ids = {
            eid for eid, ent in list(engine.entities.items())
            if ent.type == EntityType.DARK_VESSEL
        }

        obs = Observation(
            obs_id=f"obs_sar_{d['detection_id']}",
            source=SourceType.SAR,
            source_id=d["detection_id"],
            geom=Geom(lon=d["lon"], lat=d["lat"]),
            h3_cell=h3.latlng_to_cell(d["lat"], d["lon"], H3_RES),
            t=scene_t,
            attrs={
                "scene_id": scene_id,
                "rcs_db": d["rcs_db"],
                "length_m": d["length_m"],
            },
            confidence=d["confidence"],
            raw_lineage=f"sar:{scene_id}",
        )
        ent = engine.ingest(obs)

        if ent.type in (EntityType.VESSEL, EntityType.AIS_GAP):
            n_ais_matched += 1
            matched[d["detection_id"]] = ent.entity_id
        elif ent.type == EntityType.DARK_VESSEL:
            if ent.entity_id in existing_dark_ids:
                n_dark_continued += 1
            else:
                n_dark_new += 1
        # other types are unexpected — leave unset

    # Persist matched_entity_id back to the detections.
    if matched:
        with session_scope() as s:
            for det_id, eid in matched.items():
                s.execute(
                    dbm.SarDetectionRow.__table__.update()
                    .where(dbm.SarDetectionRow.detection_id == det_id)
                    .values(matched_entity_id=eid)
                )

    summary = {
        "scene_id": scene_id,
        "n_processed": len(det_payload),
        "n_ais_matched": n_ais_matched,
        "n_dark_continued": n_dark_continued,
        "n_dark_new": n_dark_new,
    }
    log.info("[%s] fusion: %s", scene_id, summary)

    # Dispatch dark-vessel alert. Best-effort: any failure is logged but
    # does not break the fusion summary.
    if n_dark_new > 0:
        try:
            import alerts
            scene_attrs = {}
            with session_scope() as s:
                scene = s.get(dbm.SarSceneRow, scene_id)
                scene_attrs = {
                    "name": (scene.attrs or {}).get("name", "") if scene else "",
                    "acquired_at": scene.acquired_at if scene else None,
                }
            if scene_attrs.get("acquired_at"):
                # Sample = the detections we just classified as dark-new.
                # We don't have a flag for that on det_payload, so re-load
                # the recently-inserted matched_entity_id IS NULL rows.
                with session_scope() as s:
                    fresh_dark_rows = s.execute(
                        sa_select(dbm.SarDetectionRow)
                        .where(dbm.SarDetectionRow.scene_id == scene_id)
                        .where(dbm.SarDetectionRow.matched_entity_id.is_(None))
                        .order_by(dbm.SarDetectionRow.rcs_db.desc())
                        .limit(alerts.MAX_DETECTIONS_IN_BODY * 2)
                    ).scalars().all()
                    sample = []
                    for r in fresh_dark_rows:
                        pt = to_shape(r.geom)
                        sample.append({
                            "lat": pt.y, "lon": pt.x,
                            "rcs_db": r.rcs_db, "length_m": r.length_m,
                            "confidence": r.confidence,
                        })
                alert_result = alerts.notify_dark_vessels(
                    scene_id=scene_id,
                    scene_name=scene_attrs.get("name", ""),
                    scene_acquired_at=scene_attrs["acquired_at"],
                    n_dark_new=n_dark_new,
                    n_dark_continued=n_dark_continued,
                    sample=sample,
                )
                summary["alert"] = alert_result
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] alert dispatch failed (non-fatal): %s",
                          scene_id, exc)
            summary["alert"] = {"skipped": f"dispatch error: {exc}"}

    return summary
