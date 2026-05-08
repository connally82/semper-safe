"""
Sentinel-2 MSI L2A optical catalog discovery (Phase 4.x of docs/roadmap.md).

Phase 1 (this module): catalog only.
  - discover_scenes(...) queries Copernicus OData for L2A products in
    the AOI within the acquisition window.
  - record_scenes(...) upserts into s2_scenes with state='discovered'.
  - main.py runs a 6h _s2_discover_loop alongside the existing
    _sar_discover_loop.

Phase 2 (follow-up):
  - download_scene_to_r2 + thumbnail extraction
  - per-detection chip lookup so the SAR detection popup can show
    a small RGB image from the closest-in-time S2 pass

Why we mirror sar.py rather than refactor:
  - The two sensors have substantially different attribute sets
    (S1 has polarization, S2 has cloud_cover) and the discover/record
    code is small. A unified abstraction would be more code, not less.
  - Keeps the diff against the working SAR code visible side-by-side.

Catalog endpoint: same OData URL as SAR.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger("s2")

ODATA_PRODUCTS = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# Same Texas AOI as SAR for now. Phase 5 lets operators define
# per-domain AOIs in config.
TEXAS_SHORELINE_WKT = (
    "POLYGON((-98 25.5, -93.5 25.5, -93.5 30.5, -98 30.5, -98 25.5))"
)

# Copernicus OData reuses the SRID=4326 wrapper for footprints.
_FOOTPRINT_RE = re.compile(r"geography'SRID=\d+;\s*(.+?)'\s*$", re.DOTALL)


def _strip_footprint(footprint_str: str) -> str:
    m = _FOOTPRINT_RE.match(footprint_str.strip())
    return m.group(1).strip() if m else footprint_str.strip()


def _attr(product: dict, name: str) -> Any:
    """Pull a typed attribute by name from an OData product. Returns None
    if the attribute is missing or empty.

    Copernicus exposes scene metadata (cloud cover, processing baseline,
    etc.) through an `Attributes` array of {Name, Value, ValueType}
    triples. cloudCover specifically is normalized as percent (0..100)
    in the L2A product.
    """
    for a in product.get("Attributes") or []:
        if a.get("Name") == name:
            return a.get("Value")
    return None


def discover_scenes(
    *,
    aoi_wkt: str = TEXAS_SHORELINE_WKT,
    since: datetime | None = None,
    until: datetime | None = None,
    product_type_substr: str = "MSIL2A",
    max_cloud_cover_pct: float | None = None,
    limit: int = 50,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Query Copernicus OData for Sentinel-2 L2A scenes matching the filter.

    Returns a list of normalized dicts (one per scene) with the fields
    we'd persist in s2_scenes:
      scene_id, name, platform, product_type, acquired_at,
      footprint_wkt, source_url, content_length_bytes, cloud_cover_pct,
      online

    Defaults: last 14 days of L2A over Texas. cloud_cover filter is
    optional — operators can filter later via the listing endpoint.
    """
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=14))

    iso_since = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_until = until.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    odata_filter = (
        "Collection/Name eq 'SENTINEL-2'"
        f" and ContentDate/Start gt {iso_since}"
        f" and ContentDate/Start lt {iso_until}"
        f" and contains(Name, '{product_type_substr}')"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')"
    )
    params = {
        "$filter": odata_filter,
        "$top": str(limit),
        "$orderby": "ContentDate/Start desc",
        # We want the Attributes array so we can pluck cloudCover.
        "$expand": "Attributes",
    }

    log.info(
        "OData search: SENTINEL-2 %s since=%s until=%s limit=%d",
        product_type_substr, iso_since, iso_until, limit,
    )
    resp = httpx.get(ODATA_PRODUCTS, params=params, timeout=timeout_s)
    resp.raise_for_status()
    products = resp.json().get("value", [])
    log.info("OData returned %d products", len(products))

    out: list[dict[str, Any]] = []
    for p in products:
        name = p.get("Name", "")
        # S2A_MSIL2A_20260506T... → platform 'S2A', product_type 'MSIL2A'
        platform = name[:3] if name.startswith("S2") else "S2?"
        m_pt = re.search(r"_(MSIL[12][AC])_", name)
        product_type = m_pt.group(1) if m_pt else product_type_substr

        # cloudCover may live as cloudCover (str/float) or be missing.
        cc_raw = _attr(p, "cloudCover")
        try:
            cloud_cover_pct = float(cc_raw) if cc_raw is not None else None
        except (TypeError, ValueError):
            cloud_cover_pct = None

        # Optional client-side cloud filter.
        if (max_cloud_cover_pct is not None
                and cloud_cover_pct is not None
                and cloud_cover_pct > max_cloud_cover_pct):
            continue

        out.append({
            "scene_id": p.get("Id"),
            "name": name,
            "platform": platform,
            "product_type": product_type,
            "acquired_at": p.get("ContentDate", {}).get("Start"),
            "footprint_wkt": _strip_footprint(p.get("Footprint", "") or ""),
            "source_url": (
                f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({p['Id']})/$value"
            ),
            "content_length_bytes": int(p.get("ContentLength") or 0),
            "cloud_cover_pct": cloud_cover_pct,
            "online": bool(p.get("Online")),
        })
    return out


def record_scenes(scenes: list[dict[str, Any]]) -> dict[str, int]:
    """Insert/upsert discovered scenes into s2_scenes with state='discovered'.

    Returns counts dict {inserted: N, skipped_existing: M}. Mirror of
    sar.record_scenes — the same psycopg3-rowcount caveat applies, so we
    diff against the existing scene_id set rather than trusting rowcount.
    """
    if not scenes:
        return {"inserted": 0, "skipped_existing": 0}

    # Lazy DB imports — keep import-time light so unit tests can import
    # this module without spinning up SQLAlchemy.
    from geoalchemy2.shape import from_shape
    from shapely import wkt as shapely_wkt
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from db import models as dbm
    from db.session import session_scope

    rows = []
    for s in scenes:
        try:
            geom = from_shape(shapely_wkt.loads(s["footprint_wkt"]), srid=4326)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping scene %s — bad footprint: %s",
                        s.get("scene_id"), exc)
            continue
        rows.append({
            "scene_id": s["scene_id"],
            "platform": s["platform"],
            "product_type": s["product_type"],
            "acquired_at": s["acquired_at"],
            "ingested_at": datetime.now(timezone.utc),
            "footprint": geom,
            "cloud_cover_pct": s.get("cloud_cover_pct"),
            "raw_url": None,
            "source_url": s["source_url"],
            "state": "discovered",
            "failure_reason": None,
            "attrs": {
                "content_length_bytes": s["content_length_bytes"],
                "online": s["online"],
                "name": s["name"],
            },
        })

    if not rows:
        return {"inserted": 0, "skipped_existing": 0}

    candidate_ids = [r["scene_id"] for r in rows]
    with session_scope() as s:
        existing = set(s.execute(
            sa_select(dbm.S2SceneRow.scene_id).where(
                dbm.S2SceneRow.scene_id.in_(candidate_ids),
            )
        ).scalars())
        new_rows = [r for r in rows if r["scene_id"] not in existing]
        if new_rows:
            s.execute(
                pg_insert(dbm.S2SceneRow)
                .values(new_rows)
                .on_conflict_do_nothing(index_elements=["scene_id"])
            )

    return {"inserted": len(new_rows), "skipped_existing": len(rows) - len(new_rows)}


# --- Download --------------------------------------------------------

PRODUCT_DOWNLOAD_URL_TPL = (
    "https://zipper.dataspace.copernicus.eu/odata/v1/Products({scene_id})/$value"
)


def download_scene_to_r2(scene_id: str, *,
                          http_chunk_size: int = 1 << 20,
                          part_size: int = 16 * 1024 * 1024,
                          timeout_s: float = 600.0) -> dict:
    """True-stream a Sentinel-2 product .SAFE.zip into Cloudflare R2.

    Same multipart-streaming pattern as sar.download_scene_to_r2 — see
    that docstring for the memory profile + abort semantics. The only
    deltas: different scene table (s2_scenes), different R2 prefix
    (s2/scenes/), and the auth singleton is shared (CDSE).

    Updates s2_scenes:
      raw_url    = r2://bucket/s2/scenes/{scene_id}.SAFE.zip on success
      state      = 'downloaded' / 'failed'
      failure_reason populated on error

    Aborts the multipart upload on any exception so we don't leak parts.
    """
    # Reuse the SAR module's CDSE auth singleton — same Copernicus account.
    from sar import auth as cdse_auth
    from db import archive
    from db import models as dbm
    from db.session import session_scope

    if not cdse_auth().is_configured():
        raise RuntimeError("Copernicus auth not configured")
    cfg = archive._r2_config()  # noqa: SLF001
    if cfg is None:
        raise RuntimeError("R2 not configured (R2_*)")

    tok = cdse_auth().token()
    if not tok:
        raise RuntimeError("Copernicus token fetch failed")

    url = PRODUCT_DOWNLOAD_URL_TPL.format(scene_id=scene_id)
    key = f"s2/scenes/{scene_id}.SAFE.zip"
    raw_url = f"r2://{cfg['bucket']}/{key}"

    with session_scope() as s:
        scene = s.get(dbm.S2SceneRow, scene_id)
        if scene is None:
            raise RuntimeError(f"s2_scenes row not found: {scene_id}")
        if scene.state == "downloaded" and scene.raw_url:
            return {"scene_id": scene_id, "skipped": "already downloaded",
                    "raw_url": scene.raw_url}

    if part_size < 5 * 1024 * 1024:
        raise ValueError("S3 part_size must be >= 5 MB")

    client = archive._r2_client(cfg)  # noqa: SLF001
    bucket = cfg["bucket"]

    log.info("starting multipart upload for S2 scene %s -> %s",
             scene_id, raw_url)
    create = client.create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/zip",
    )
    upload_id = create["UploadId"]

    parts: list[dict] = []
    bytes_seen = 0
    part_buf = bytearray()
    part_number = 1

    def _flush_part(buf: bytearray, n: int) -> None:
        if not buf:
            return
        resp = client.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id,
            PartNumber=n, Body=bytes(buf),
        )
        parts.append({"PartNumber": n, "ETag": resp["ETag"]})
        log.info("uploaded S2 part %d (%d bytes) for scene %s",
                 n, len(buf), scene_id)

    try:
        with httpx.stream(
            "GET", url, headers={"Authorization": f"Bearer {tok}"},
            timeout=timeout_s, follow_redirects=True,
        ) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(http_chunk_size):
                if not chunk:
                    continue
                part_buf.extend(chunk)
                bytes_seen += len(chunk)
                while len(part_buf) >= part_size:
                    head = part_buf[:part_size]
                    del part_buf[:part_size]
                    _flush_part(head, part_number)
                    part_number += 1
        if part_buf:
            _flush_part(part_buf, part_number)
            part_number += 1

        if not parts:
            raise RuntimeError("download produced 0 bytes")

        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception as exc:  # noqa: BLE001
        try:
            client.abort_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
            )
        except Exception as abort_exc:  # noqa: BLE001
            log.warning("abort_multipart_upload also failed for %s: %s",
                        scene_id, abort_exc)
        with session_scope() as s:
            scene = s.get(dbm.S2SceneRow, scene_id)
            if scene is not None:
                scene.state = "failed"
                scene.failure_reason = f"{type(exc).__name__}: {exc}"[:500]
        log.exception("S2 download failed for %s", scene_id)
        raise

    with session_scope() as s:
        scene = s.get(dbm.S2SceneRow, scene_id)
        if scene is not None:
            scene.raw_url = raw_url
            scene.state = "downloaded"
            scene.failure_reason = None
            scene.attrs = {**(scene.attrs or {}),
                           "bytes_uploaded": bytes_seen,
                           "parts": len(parts)}

    log.info("downloaded S2 scene %s -> %s (%d bytes, %d parts)",
             scene_id, raw_url, bytes_seen, len(parts))
    return {"scene_id": scene_id, "raw_url": raw_url,
            "bytes": bytes_seen, "parts": len(parts), "key": key}


# --- Best-match lookup ----------------------------------------------

def find_nearest_s2_for_point(lat: float, lon: float,
                               near_t: datetime,
                               *,
                               max_days: int = 3,
                               max_cloud_pct: float = 40.0) -> str | None:
    """Find the S2 scene whose footprint CONTAINS (lat, lon) and whose
    acquired_at is closest to near_t, within ±max_days, ≤max_cloud_pct.

    The right matcher for per-detection chip extraction. Earlier
    versions matched by SAR-scene footprint overlap, but a Sentinel-1
    GRDH scene is ~250 km wide, so an S2 tile that intersects the SAR
    bounds doesn't necessarily contain the actual detection point.
    """
    from datetime import timedelta
    from sqlalchemy import select as sa_select, or_
    from geoalchemy2 import functions as gfn
    from geoalchemy2.shape import from_shape
    from shapely.geometry import Point

    from db import models as dbm
    from db.session import session_scope

    window_lo = near_t - timedelta(days=max_days)
    window_hi = near_t + timedelta(days=max_days)
    pt = from_shape(Point(lon, lat), srid=4326)

    with session_scope() as s:
        rows = s.execute(
            sa_select(dbm.S2SceneRow.scene_id, dbm.S2SceneRow.acquired_at)
            .where(
                dbm.S2SceneRow.acquired_at >= window_lo,
                dbm.S2SceneRow.acquired_at <= window_hi,
                or_(
                    dbm.S2SceneRow.cloud_cover_pct.is_(None),
                    dbm.S2SceneRow.cloud_cover_pct <= max_cloud_pct,
                ),
                gfn.ST_Contains(dbm.S2SceneRow.footprint, pt),
            )
        ).all()
    if not rows:
        return None
    rows.sort(key=lambda r: abs((r.acquired_at - near_t).total_seconds()))
    return rows[0].scene_id


def find_nearest_s2_for_sar_scene(sar_scene_id: str, *,
                                   max_days: int = 3,
                                   max_cloud_pct: float = 40.0) -> str | None:
    """Find the S2 scene whose acquired_at is closest to a SAR scene's
    acquired_at AND whose footprint overlaps. Used to suggest visual
    confirmation imagery for SAR detections.

    Returns the S2 scene_id or None.
    """
    from datetime import timedelta
    from sqlalchemy import select as sa_select, or_, func
    from geoalchemy2.functions import ST_Intersects

    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        sar_scene = s.get(dbm.SarSceneRow, sar_scene_id)
        if sar_scene is None:
            return None
        sar_t = sar_scene.acquired_at
        window_lo = sar_t - timedelta(days=max_days)
        window_hi = sar_t + timedelta(days=max_days)

        rows = s.execute(
            sa_select(dbm.S2SceneRow.scene_id, dbm.S2SceneRow.acquired_at)
            .where(
                dbm.S2SceneRow.acquired_at >= window_lo,
                dbm.S2SceneRow.acquired_at <= window_hi,
                or_(
                    dbm.S2SceneRow.cloud_cover_pct.is_(None),
                    dbm.S2SceneRow.cloud_cover_pct <= max_cloud_pct,
                ),
                # Footprints overlap.
                ST_Intersects(dbm.S2SceneRow.footprint, sar_scene.footprint),
            )
        ).all()
        if not rows:
            return None
        # Pick the one closest in time to the SAR pass.
        rows.sort(key=lambda r: abs((r.acquired_at - sar_t).total_seconds()))
        return rows[0].scene_id
