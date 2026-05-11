"""
NIFC (National Interagency Fire Center) active-incident client.

Pulls the WFIGS (Wildland Fire Integrated Geospatial Solution)
incident-locations feature service, which is the canonical real-time
list of US wildfire incidents reported by federal/state/local fire
agencies. Free, no API key, ArcGIS REST GeoJSON format.

Feature service:
  https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/
    WFIGS_Incident_Locations_Current/FeatureServer/0/query

Notable fields per feature:
  - IncidentName, IncidentTypeCategory ('WF' = Wildfire)
  - FireDiscoveryDateTime, FireBehaviorGeneral
  - IncidentSize (acres), PercentContained
  - POOResponsibleAgency, POOState
  - InitialResponseAcres, EstimatedCostToDate
  - geometry: Point (POO = Point of Origin)

We filter to active WF incidents inside the Western US AOI; perimeter
polygons live in a separate WFIGS_Incident_Perimeters service that we
hit only when an incident point is selected (saves bandwidth).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger("nifc")


# Same AOI clamp the wildfire frontend uses — Western US.
AOI_LON_MIN = -125.0
AOI_LON_MAX = -100.0
AOI_LAT_MIN = 30.0
AOI_LAT_MAX = 50.0

_INCIDENT_QUERY_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
_PERIMETER_QUERY_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Perimeters_Current/FeatureServer/0/query"
)


def _aoi_envelope() -> str:
    """ArcGIS geometry param: Western-US bounding-box envelope."""
    return (
        f"{AOI_LON_MIN},{AOI_LAT_MIN},"
        f"{AOI_LON_MAX},{AOI_LAT_MAX}"
    )


def fetch_active_incidents(limit: int = 200) -> list[dict]:
    """Pull current active wildfire incidents in the Western US AOI.

    Returns a list of normalized dicts:
      {incident_id, name, lon, lat, discovered_at, size_acres,
       contained_pct, agency, state, behavior, type}
    Sorted by size_acres desc so the biggest incidents land first in
    a frontend-cap'd render.
    """
    import httpx

    params = {
        "where": "IncidentTypeCategory='WF'",
        "outFields": (
            "IncidentName,FireDiscoveryDateTime,IncidentSize,"
            "PercentContained,POOResponsibleAgency,POOState,"
            "FireBehaviorGeneral,FireCause,UniqueFireIdentifier"
        ),
        "geometry": _aoi_envelope(),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(limit),
        "orderByFields": "IncidentSize DESC",
    }

    with httpx.Client(timeout=20.0) as client:
        r = client.get(_INCIDENT_QUERY_URL, params=params)
        r.raise_for_status()
        data = r.json()

    out: list[dict] = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        discovered_ms = props.get("FireDiscoveryDateTime")
        discovered_iso: str | None = None
        if isinstance(discovered_ms, (int, float)) and discovered_ms > 0:
            try:
                discovered_iso = datetime.fromtimestamp(
                    discovered_ms / 1000.0, tz=timezone.utc
                ).isoformat()
            except (OverflowError, OSError):
                pass

        out.append({
            "incident_id": props.get("UniqueFireIdentifier")
                          or f"nifc_{abs(hash((lon, lat)))%10**10}",
            "name": props.get("IncidentName") or "Unnamed incident",
            "lon": lon,
            "lat": lat,
            "discovered_at": discovered_iso,
            "size_acres": props.get("IncidentSize"),
            "contained_pct": props.get("PercentContained"),
            "agency": props.get("POOResponsibleAgency"),
            "state": props.get("POOState"),
            "behavior": props.get("FireBehaviorGeneral"),
            "cause": props.get("FireCause"),
        })
    return out


def fetch_incident_perimeter(incident_id: str) -> dict | None:
    """Pull the polygon perimeter for one incident, if NIFC has one.

    Many small fires don't have a perimeter mapped yet (typically
    requires an air-attack-mapped flight). Returns GeoJSON Polygon
    or null.
    """
    import httpx

    safe_id = incident_id.replace("'", "''")
    params = {
        "where": f"UniqueFireIdentifier='{safe_id}'",
        "outFields": "UniqueFireIdentifier,IncidentName,GISAcres",
        "f": "geojson",
        "outSR": "4326",
        "resultRecordCount": "1",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(_PERIMETER_QUERY_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("perimeter fetch failed for %s: %s", incident_id, exc)
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    return feats[0]
