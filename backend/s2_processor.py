"""
Sentinel-2 RGB chip extractor (Phase 4.x sensor-stack expansion).

Use case:
  An operator clicks a SAR detection, wants a daylight photo to confirm
  whether it is a real vessel. We pull the closest-in-time low-cloud
  Sentinel-2 L2A scene that overlaps the detection, read a small window
  around the lat/lon, composite RGB, and serve a JPEG chip the popup
  can render inline.

Why a chip rather than a full scene visualization:
  S2 L2A SAFE.zip is ~600 MB-1.2 GB; the operator only needs a 1-2 km
  square chip. /vsizip+/vsis3 + COG range reads pull just those bytes,
  so total transfer is single-digit MB even with a fresh detection.

Pipeline:
  1. Look up the cached chip in R2 (sar_chips/{detection_id}.jpg) and
     return it if it exists.
  2. Else, find the nearest S2 scene to the detection (same scene the
     find_nearest_s2_for_sar_scene helper returns).
  3. Make sure that S2 scene has been downloaded to R2 (state='downloaded').
     If not, surface a 202-equivalent so the caller can kick the
     download asynchronously.
  4. Read B04 (Red), B03 (Green), B02 (Blue) at 10 m via /vsizip+/vsis3
     centered on the detection lat/lon. Window size = half_size_m / 10
     pixels each side.
  5. Linear-stretch each band to 8-bit using percentile-based scaling
     (P2..P98) for visual contrast on water scenes (mostly dark).
  6. Composite RGB, encode as JPEG q=85, upload to R2 cache.
  7. Return JPEG bytes (caller serves with Content-Type image/jpeg).

What this version does NOT do (Phase 5):
  - Atmospheric correction (L2A is already BOA, so we trust it).
  - Pansharpening (10 m bands are already at the resolution we want).
  - Cross-scene mosaicking when a detection sits on a tile boundary.
  - Auto-download of the S2 scene during chip request (today the caller
    has to ensure the scene is downloaded; we'll wire BackgroundTask
    auto-fetch in Phase 4.y).

Memory profile:
  Chip is ~150x150 px per band (1.5 km / 10 m), uint16 → ~50 KB per
  band, three bands plus JPEG buffer < 1 MB peak. Negligible vs CFAR.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
from typing import Any

log = logging.getLogger("s2_processor")

# Default chip size in meters (radius from the centroid). 1500 m gives
# a ~3 km square — comfortably wider than even a Capesize bulk carrier
# (~290 m) and tight enough to fit in a popup.
DEFAULT_HALF_SIZE_M = 1500.0
S2_PIXEL_M = 10.0   # B02/B03/B04/B08 are 10 m
JPEG_QUALITY = 85


def _r2_chip_key(detection_id: str) -> str:
    return f"sar_chips/{detection_id}.jpg"


def _read_cached_chip(detection_id: str) -> bytes | None:
    """If we've already generated a chip for this detection, return it.
    R2 is content-addressable; chips are immutable once written."""
    from db import archive
    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        return None
    client = archive._r2_client(cfg)  # noqa: SLF001
    try:
        resp = client.get_object(Bucket=cfg["bucket"],
                                 Key=_r2_chip_key(detection_id))
        return resp["Body"].read()
    except Exception:  # noqa: BLE001
        return None


def _put_chip(detection_id: str, body: bytes) -> str:
    from db import archive
    cfg = archive._r2_config()  # noqa: SLF001
    client = archive._r2_client(cfg)  # noqa: SLF001
    key = _r2_chip_key(detection_id)
    client.put_object(Bucket=cfg["bucket"], Key=key, Body=body,
                      ContentType="image/jpeg")
    return f"r2://{cfg['bucket']}/{key}"


def _find_band_path(scene_id: str, band: str) -> str:
    """Build the GDAL /vsizip+/vsis3 path for a Sentinel-2 L2A 10m band.

    L2A SAFE layout for tile T15RVK / 20260506:
      .../GRANULE/L2A_T15RVK_A032455_20260506T164701/
        IMG_DATA/R10m/T15RVK_20260506T164701_B02_10m.jp2
        IMG_DATA/R10m/T15RVK_20260506T164701_B03_10m.jp2
        IMG_DATA/R10m/T15RVK_20260506T164701_B04_10m.jp2

    The exact internal path depends on the granule + tile id which we
    don't know without listing the zip. We use _list_band_in_zip below
    to discover the right one.
    """
    raise NotImplementedError("use _list_band_in_zip + build path from result")


def _list_band_in_zip(scene_id: str, band: str) -> str | None:
    """Walk the SAFE.zip central directory and return the inner path of
    the requested 10 m band's JP2 file (e.g. ".../IMG_DATA/R10m/..._B04_10m.jp2").

    We rely on the same byte-range trick we used for the SAR annotation
    XML — the SAFE.zip uses STORED (uncompressed) entries so we can
    enumerate them via the central directory without downloading the
    whole archive.
    """
    import struct
    from urllib.request import urlopen, Request

    from db import archive

    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        return None
    client = archive._r2_client(cfg)  # noqa: SLF001
    key = f"s2/scenes/{scene_id}.SAFE.zip"

    # HEAD to get content length.
    h = client.head_object(Bucket=cfg["bucket"], Key=key)
    size = int(h["ContentLength"])

    # Sign a presigned URL so urllib + Range works without us re-implementing
    # SigV4. ExpiresIn is short — chip extraction completes in seconds.
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg["bucket"], "Key": key},
        ExpiresIn=300,
    )

    tail = urlopen(
        Request(url, headers={"Range": f"bytes={max(0, size-65536)}-{size-1}"}),
        timeout=20,
    ).read()
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        log.warning("no EOCD in S2 zip for %s", scene_id)
        return None
    cd_size = struct.unpack("<I", tail[i+12:i+16])[0]
    cd_off = struct.unpack("<I", tail[i+16:i+20])[0]
    cd = urlopen(
        Request(url, headers={"Range": f"bytes={cd_off}-{cd_off+cd_size-1}"}),
        timeout=30,
    ).read()

    needle = f"_{band}_10m.jp2"
    p = 0
    while p < len(cd):
        if cd[p:p+4] != b"PK\x01\x02":
            break
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[p+28:p+34])
        name = cd[p+46:p+46+name_len].decode("utf-8", errors="replace")
        if needle in name and "/IMG_DATA/R10m/" in name:
            return name
        p += 46 + name_len + extra_len + comment_len
    return None


def _open_band(scene_id: str, band: str):
    """Open the 10 m band as a rasterio dataset via /vsizip+/vsis3/."""
    import rasterio  # heavy import — lazy

    inner = _list_band_in_zip(scene_id, band)
    if inner is None:
        raise RuntimeError(f"band {band} not found in S2 scene {scene_id}")
    from db import archive
    cfg = archive._r2_config()  # noqa: SLF001
    # Same env vars as sar_processor; idempotent to set repeatedly.
    os.environ.update({
        "AWS_ACCESS_KEY_ID":     cfg["access_key_id"],
        "AWS_SECRET_ACCESS_KEY": cfg["secret_access_key"],
        "AWS_S3_ENDPOINT":       f"{cfg['account_id']}.r2.cloudflarestorage.com",
        "AWS_VIRTUAL_HOSTING":   "FALSE",
        "AWS_HTTPS":             "YES",
        "AWS_REGION":            "auto",
        "VSI_CACHE":             "TRUE",
        "VSI_CACHE_SIZE":        str(64 << 20),
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tiff,.tif,.zip,.jp2",
    })
    path = (f"/vsizip//vsis3/{cfg['bucket']}/s2/scenes/"
            f"{scene_id}.SAFE.zip/{inner}")
    return rasterio.open(path)


def _stretch_to_uint8(arr, lo_pct: float = 2.0, hi_pct: float = 98.0):
    """Linear contrast stretch with percentile clip → uint8 (0..255)."""
    import numpy as np
    a = arr.astype("float32")
    lo = float(np.percentile(a, lo_pct))
    hi = float(np.percentile(a, hi_pct))
    if hi <= lo:
        return np.zeros_like(a, dtype="uint8")
    out = np.clip((a - lo) / (hi - lo), 0.0, 1.0) * 255.0
    return out.astype("uint8")


def extract_chip(detection_id: str, scene_id: str,
                 lat: float, lon: float, *,
                 half_size_m: float = DEFAULT_HALF_SIZE_M) -> bytes:
    """Generate (or read cached) the RGB chip for one detection.

    Returns JPEG bytes. Caller is responsible for setting
    Content-Type: image/jpeg in the HTTP response.
    """
    cached = _read_cached_chip(detection_id)
    if cached is not None:
        log.info("chip cache hit for %s (%d bytes)", detection_id, len(cached))
        return cached

    import numpy as np
    from PIL import Image
    import rasterio
    from rasterio.windows import from_bounds

    half_deg_lat = half_size_m / 111_000.0
    half_deg_lon = half_size_m / (111_000.0 * math.cos(math.radians(lat)))
    minx, maxx = lon - half_deg_lon, lon + half_deg_lon
    miny, maxy = lat - half_deg_lat, lat + half_deg_lat

    log.info("[%s] extracting S2 chip from scene %s @ (%.4f,%.4f) ±%dm",
             detection_id, scene_id, lat, lon, int(half_size_m))

    chans = []
    for band in ("B04", "B03", "B02"):  # R, G, B
        with _open_band(scene_id, band) as src:
            # S2 L2A bands are in UTM zones (EPSG:326XX). The window is
            # given in the dataset's CRS, so transform our lat/lon bbox.
            from rasterio.warp import transform_bounds
            l, b, r, t = transform_bounds(
                "EPSG:4326", src.crs, minx, miny, maxx, maxy, densify_pts=21,
            )
            window = from_bounds(l, b, r, t, src.transform)
            arr = src.read(1, window=window, boundless=True, fill_value=0)
            chans.append(arr)

    # All three should be the same shape (10 m bands at the same tile).
    h = min(c.shape[0] for c in chans)
    w = min(c.shape[1] for c in chans)
    chans = [c[:h, :w] for c in chans]
    if h == 0 or w == 0:
        raise RuntimeError(
            f"empty chip for {detection_id} — chip window outside scene "
            f"footprint (lat={lat}, lon={lon}, scene={scene_id})"
        )

    rgb = np.dstack([_stretch_to_uint8(c) for c in chans])
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    body = buf.getvalue()

    try:
        _put_chip(detection_id, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("chip cache write failed (returning anyway): %s", exc)

    log.info("[%s] chip ready (%d bytes, %dx%d px)",
             detection_id, len(body), w, h)
    return body
