"""
Smoke-plume modeling for active wildfire incidents.

NOAA HRRR-Smoke is a 3-km gridded forecast of vertically-integrated
PM2.5 over the CONUS — the canonical source. It's published as GRIB2
on NOMADS; parsing GRIB2 from a backend pulls in a heavy dependency
chain (cfgrib / xarray / eccodes) we don't want to ship for the demo.

For now we MODEL plumes geometrically from each active NIFC incident:
  - Wind direction at the incident comes from the nearest risk-grid
    cell (which has peak_wind_ms; bearing comes from the gridpoint
    forecast's windDirection field — we pull it in this module).
  - Plume length scales with sqrt(size_acres) × wind_speed.
  - Plume width spreads at a fixed 25° half-angle.
  - Output: a triangular GeoJSON Polygon downwind of the incident.

The same dict shape would come back from a real HRRR-Smoke pull, so
the frontend rendering doesn't change when we upgrade the source.
"""

from __future__ import annotations

import math
import logging

log = logging.getLogger("wildfire_smoke")

PLUME_HALF_ANGLE_DEG = 25.0
MIN_PLUME_LENGTH_KM = 8.0
MAX_PLUME_LENGTH_KM = 240.0
DEFAULT_WIND_BEARING_DEG = 90.0    # eastward if we can't get a forecast
DEFAULT_WIND_MS = 4.0

_USER_AGENT = "semper-safe/0.1 (https://sempersafe.live; ops@sempersafe.live)"


def _bearing_for_point(lon: float, lat: float, client) -> tuple[float, float]:
    """Best-effort NWS forecast bearing + wind speed (m/s) at (lon, lat).

    Returns (DEFAULT_WIND_BEARING_DEG, DEFAULT_WIND_MS) on any failure.
    Hits the same gridpoint as wildfire_risk so the responses cache
    well at the NWS edge.
    """
    try:
        r1 = client.get(f"https://api.weather.gov/points/{lat},{lon}")
        r1.raise_for_status()
        grid_url = (r1.json().get("properties") or {}).get("forecastGridData")
        if not grid_url:
            return DEFAULT_WIND_BEARING_DEG, DEFAULT_WIND_MS
        r2 = client.get(grid_url)
        r2.raise_for_status()
        props = (r2.json() or {}).get("properties") or {}
        dirs = (props.get("windDirection") or {}).get("values") or []
        winds = (props.get("windSpeed") or {}).get("values") or []
        wind_dir = (dirs[0].get("value") if dirs else None) or DEFAULT_WIND_BEARING_DEG
        wind_kmh = (winds[0].get("value") if winds else None) or (DEFAULT_WIND_MS * 3.6)
        return float(wind_dir), float(wind_kmh) / 3.6
    except Exception as exc:  # noqa: BLE001
        log.debug("wind fetch failed at (%s, %s): %s", lon, lat, exc)
        return DEFAULT_WIND_BEARING_DEG, DEFAULT_WIND_MS


def _destination_point(lon: float, lat: float,
                       bearing_deg: float, distance_km: float
                       ) -> tuple[float, float]:
    """Standard great-circle destination from origin + bearing + distance."""
    R = 6371.0
    br = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    dist = distance_km / R
    p2 = math.asin(
        math.sin(p1) * math.cos(dist)
        + math.cos(p1) * math.sin(dist) * math.cos(br)
    )
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(dist) * math.cos(p1),
        math.cos(dist) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(l2), math.degrees(p2)


def _plume_polygon(lon: float, lat: float, bearing_deg: float,
                   length_km: float) -> dict:
    """Build a triangular wedge polygon downwind of (lon, lat)."""
    # Bearing in NWS API is the direction the wind is COMING FROM
    # (meteorological convention). We want the direction it's going
    # TO — add 180°.
    to_bearing = (bearing_deg + 180.0) % 360.0
    half = PLUME_HALF_ANGLE_DEG
    a = _destination_point(lon, lat, (to_bearing - half) % 360, length_km)
    b = _destination_point(lon, lat, (to_bearing + half) % 360, length_km)
    tip = _destination_point(lon, lat, to_bearing, length_km * 1.1)
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat],
            list(a),
            list(tip),
            list(b),
            [lon, lat],
        ]],
    }


def compute_plumes(incidents: list[dict]) -> list[dict]:
    """Build a synthetic plume polygon per active incident.

    Skips incidents missing position or size_acres. Caps plume length
    at MAX_PLUME_LENGTH_KM to keep large fires (>100k acres) from
    drawing plumes off the entire map.
    """
    import httpx

    out: list[dict] = []
    if not incidents:
        return out

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    with httpx.Client(timeout=15.0, headers=headers) as client:
        for inc in incidents:
            lon, lat = inc.get("lon"), inc.get("lat")
            size = inc.get("size_acres")
            if lon is None or lat is None:
                continue
            try:
                acres = float(size) if size is not None else 100.0
            except (TypeError, ValueError):
                acres = 100.0
            bearing, wind_ms = _bearing_for_point(lon, lat, client)
            length_km = max(MIN_PLUME_LENGTH_KM, math.sqrt(acres) * wind_ms * 0.5)
            length_km = min(length_km, MAX_PLUME_LENGTH_KM)
            out.append({
                "incident_id": inc.get("incident_id"),
                "incident_name": inc.get("name"),
                "bearing_from_deg": round(bearing, 1),
                "wind_speed_ms":    round(wind_ms, 1),
                "length_km":        round(length_km, 1),
                "size_acres":       acres,
                "geometry": _plume_polygon(lon, lat, bearing, length_km),
            })
    return out
