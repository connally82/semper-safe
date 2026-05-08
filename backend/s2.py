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
