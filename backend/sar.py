"""
Sentinel-1 SAR ingestion (Phase 4 of docs/roadmap.md).

Pipeline stages:
  1. discover_scenes(bbox, since) — query Copernicus OData catalog for
     Sentinel-1 IW GRDH scenes intersecting the AOI. Public, no auth.
  2. record_scenes(...)            [Phase 4.1] insert into sar_scenes
  3. download_scene(...)           [Phase 4.2] requires Copernicus auth
  4. detect_cfar(...)              [Phase 4.3] NumPy CFAR per the
                                   roadmap's reference paper
  5. fuse_detections(...)          [Phase 4.4] hand each detection to
                                   maritime.ingest as a SourceType.SAR
                                   observation (engine handles match
                                   vs dark_vessel since Phase 1)

Catalog endpoint choice:
  Copernicus exposes both a STAC API (catalogue.dataspace.copernicus.eu/stac)
  and an OData API (.../odata/v1/). The STAC endpoint omits direct
  Sentinel collections — only Contributing Missions are listed there.
  OData is the canonical Sentinel-1 catalog and the only one that
  returns data for our query, so we use it.

This commit ships discover_scenes + record_scenes + footprint parsing.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

log = logging.getLogger("sar")


ODATA_PRODUCTS = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# Texas-shoreline AOI per memory/semper_safe_aoi.md.
# OData wants a WKT polygon string.
TEXAS_SHORELINE_WKT = (
    "POLYGON((-98 25.5, -93.5 25.5, -93.5 30.5, -98 30.5, -98 25.5))"
)


# Copernicus emits Footprint as: geography'SRID=4326;POLYGON ((x y, x y, ...))'
# The POLYGON / MULTIPOLYGON portion is plain WKT — strip the prefix.
_FOOTPRINT_RE = re.compile(r"geography'SRID=\d+;\s*(.+?)'\s*$", re.DOTALL)


def _strip_footprint(footprint_str: str) -> str:
    m = _FOOTPRINT_RE.match(footprint_str.strip())
    return m.group(1).strip() if m else footprint_str.strip()


def discover_scenes(
    *,
    aoi_wkt: str = TEXAS_SHORELINE_WKT,
    since: datetime | None = None,
    until: datetime | None = None,
    sensor_mode: str = "IW",
    product_type_substr: str = "IW_GRDH",
    limit: int = 50,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Query Copernicus OData for Sentinel-1 scenes matching the filter.

    Returns list of normalized dicts (one per scene) with the fields
    we'd persist in sar_scenes:
      scene_id, name, platform, sensor_mode, polarization, acquired_at,
      footprint_wkt, source_url, content_length_bytes, online

    Defaults: last 14 days of IW GRDH (high-res ground-range-detected,
    the standard product type for vessel detection) over Texas shoreline.
    """
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=14))

    iso_since = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_until = until.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    odata_filter = (
        "Collection/Name eq 'SENTINEL-1'"
        f" and ContentDate/Start gt {iso_since}"
        f" and ContentDate/Start lt {iso_until}"
        f" and contains(Name, '{product_type_substr}')"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')"
    )
    params = {
        "$filter": odata_filter,
        "$top": str(limit),
        "$orderby": "ContentDate/Start desc",
    }

    log.info(
        "OData search: SENTINEL-1 %s since=%s until=%s limit=%d",
        product_type_substr, iso_since, iso_until, limit,
    )
    resp = httpx.get(ODATA_PRODUCTS, params=params, timeout=timeout_s)
    resp.raise_for_status()
    products = resp.json().get("value", [])
    log.info("OData returned %d products", len(products))

    out: list[dict[str, Any]] = []
    for p in products:
        name = p.get("Name", "")
        # Polarization + platform live in the product name. Defensive parse.
        platform = name[:3] if name.startswith("S1") else "S1?"
        # Polarization codes: 1SDV (dual VV+VH), 1SSV (single VV), etc.
        pol_match = re.search(r"_1S([A-Z]{2})_", name)
        pol_code = pol_match.group(1) if pol_match else "?"
        polarization = {
            "DV": "VV+VH", "DH": "HH+HV", "SV": "VV", "SH": "HH",
        }.get(pol_code, pol_code)

        out.append({
            "scene_id": p.get("Id"),
            "name": name,
            "platform": platform,
            "sensor_mode": sensor_mode,
            "polarization": polarization,
            "acquired_at": p.get("ContentDate", {}).get("Start"),
            "footprint_wkt": _strip_footprint(p.get("Footprint", "") or ""),
            "source_url": (
                f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({p['Id']})/$value"
            ),
            "content_length_bytes": int(p.get("ContentLength") or 0),
            "online": bool(p.get("Online")),
        })
    return out


def record_scenes(scenes: list[dict[str, Any]]) -> dict[str, int]:
    """Insert/upsert discovered scenes into sar_scenes with state='discovered'.

    Returns counts dict {inserted: N, skipped_existing: M}.
    Idempotent against re-running discover_scenes — existing scene_ids
    are left untouched.
    """
    if not scenes:
        return {"inserted": 0, "skipped_existing": 0}

    # Imported lazily so importing sar.py doesn't pull DB stack.
    from geoalchemy2.shape import from_shape
    from shapely import wkt as shapely_wkt
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
            "sensor_mode": s["sensor_mode"],
            "polarization": s["polarization"],
            "acquired_at": s["acquired_at"],
            "ingested_at": datetime.now(timezone.utc),
            "footprint": geom,
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

    # psycopg3's rowcount on bulk INSERT-ON-CONFLICT is unreliable
    # (returns -1). Diff against the existing scene_id set instead.
    from sqlalchemy import select as sa_select

    candidate_ids = [r["scene_id"] for r in rows]
    with session_scope() as s:
        existing = set(s.execute(
            sa_select(dbm.SarSceneRow.scene_id).where(
                dbm.SarSceneRow.scene_id.in_(candidate_ids),
            )
        ).scalars())
        new_rows = [r for r in rows if r["scene_id"] not in existing]
        if new_rows:
            s.execute(
                pg_insert(dbm.SarSceneRow)
                .values(new_rows)
                .on_conflict_do_nothing(index_elements=["scene_id"])
            )

    return {"inserted": len(new_rows), "skipped_existing": len(rows) - len(new_rows)}


def _gen_detection_id() -> str:
    return f"sard_{uuid.uuid4().hex[:12]}"
