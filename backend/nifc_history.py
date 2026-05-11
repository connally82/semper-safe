"""
Historical wildfire perimeters — NIFC WFIGS_Interagency_Perimeters.

NIFC's WFIGS suite includes a "Past 3 Years" interagency perimeter
service that covers every fire ≥10 acres in the US going back to
2022. We pull it once on startup + once per day, cached in the
wildfire refresh loop.

Why this matters for prevention: an area that hasn't burned in
decades carries the highest fuel load. The operator can compare
today's high-HDW cells against historical perimeters and pick out
the zones that are BOTH high-risk and unburned-in-living-memory.

The full feed for the Western US AOI is ~5000 features. We trim to
fires ≥1000 acres (~500 features) to keep the bundle reasonable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("nifc_history")

AOI_LON_MIN, AOI_LON_MAX = -125.0, -100.0
AOI_LAT_MIN, AOI_LAT_MAX = 30.0, 50.0

_PERIMETER_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters/FeatureServer/0/query"
)

MIN_ACRES = 1000.0


def fetch_historical_perimeters(years_back: int = 3, limit: int = 600) -> list[dict]:
    """Pull recent (past N years) wildfire perimeters in the AOI.

    Returns a list of GeoJSON Features with normalized properties:
      {fire_name, fire_year, acres, agency, geometry}
    Sorted by acres desc so the biggest fires come first in a
    bandwidth-capped render.
    """
    import httpx

    cutoff = datetime.now(timezone.utc).replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    cutoff = cutoff.replace(year=cutoff.year - years_back)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    params = {
        "where": (
            f"GISAcres>={MIN_ACRES} AND "
            f"FireDiscoveryDateTime>={cutoff_ms}"
        ),
        "outFields": (
            "IncidentName,FireYear,GISAcres,POOResponsibleAgency"
        ),
        "geometry":
            f"{AOI_LON_MIN},{AOI_LAT_MIN},{AOI_LON_MAX},{AOI_LAT_MAX}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(limit),
        "orderByFields": "GISAcres DESC",
    }

    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.get(_PERIMETER_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("NIFC historical perimeter fetch failed: %s", exc)
        return []

    out = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if not geom:
            continue
        out.append({
            "fire_name":  props.get("IncidentName") or "Unnamed",
            "fire_year":  props.get("FireYear"),
            "acres":      props.get("GISAcres"),
            "agency":     props.get("POOResponsibleAgency"),
            "geometry":   geom,
        })
    return out
