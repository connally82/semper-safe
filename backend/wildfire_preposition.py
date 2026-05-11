"""
Pre-position recommendation engine for wildfire prevention.

Given the current state — ignition-risk grid (HDW per cell), active
NIFC incidents, and a configured set of fire-asset positions — this
module recommends where to stage resources for the next 24 hours.

Algorithm (simple but defensible for a demo):

  1. For each grid cell with risk_score >= STAGE_RISK_THRESHOLD,
     compute a "uncovered risk" = risk_score × (1 - coverage),
     where coverage decays linearly with distance to the nearest
     fire asset (out to MAX_COVERAGE_KM).
  2. Cells that already have an active NIFC incident within
     INCIDENT_DRAW_RADIUS_KM are skipped — assets in that area
     are already drawn to the fire.
  3. The top STAGE_LIMIT cells by uncovered risk become staging
     recommendations. For each, suggest the nearest fire asset to
     reposition.

Output entries:
  {
    id, lon, lat, score (uncovered_risk),
    nearest_asset { name, type, lon, lat, distance_km },
    rationale (human-readable summary),
  }

The asset catalog is intentionally small (12 facilities — CAL FIRE
ACCs and USFS Region 5 air-attack bases) so the demo runs without
a real assets-management integration.
"""

from __future__ import annotations

import math
import logging
import hashlib
from typing import Iterable

log = logging.getLogger("wildfire_preposition")

# Tunables — exposed for testability.
STAGE_RISK_THRESHOLD = 0.45        # only stage where risk_score >= this
INCIDENT_DRAW_RADIUS_KM = 60.0     # cells within this of an incident are skipped
MAX_COVERAGE_KM = 80.0             # asset's coverage tails off to 0 at this range
STAGE_LIMIT = 6                    # max prepositions per refresh tick


# Curated catalog of fire assets across the Western US.
# (lon, lat, name, type)
ASSETS: list[dict] = [
    # CAL FIRE Air Attack Centers (ACC) — fixed-wing tanker bases.
    {"lon": -120.555, "lat": 38.230, "name": "Grass Valley AAB",  "type": "tanker"},
    {"lon": -120.638, "lat": 36.738, "name": "Hollister AAB",     "type": "tanker"},
    {"lon": -118.358, "lat": 34.196, "name": "Hemet-Ryan AAB",    "type": "tanker"},
    {"lon": -120.694, "lat": 35.218, "name": "Paso Robles AAB",   "type": "tanker"},
    {"lon": -122.252, "lat": 40.150, "name": "Redding AAB",       "type": "tanker"},
    # USFS Region 5/6 air-attack bases.
    {"lon": -120.642, "lat": 38.555, "name": "Boise SmokeJumper", "type": "smokejumper"},
    {"lon": -111.652, "lat": 35.139, "name": "Flagstaff Tanker",  "type": "tanker"},
    {"lon": -116.215, "lat": 43.567, "name": "Boise IAB",         "type": "smokejumper"},
    {"lon": -122.286, "lat": 47.450, "name": "Boeing Field 8E",   "type": "helitack"},
    # CAL FIRE strategic engine staging.
    {"lon": -121.485, "lat": 38.575, "name": "Sacramento HQ",     "type": "engine"},
    {"lon": -118.243, "lat": 34.052, "name": "Los Angeles HQ",    "type": "engine"},
    # Wide-area patrol.
    {"lon": -115.470, "lat": 36.103, "name": "Las Vegas Ops",     "type": "patrol"},
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _nearest_asset(lon: float, lat: float) -> tuple[dict, float]:
    """Find the closest fire asset to a cell."""
    best = None
    best_km = float("inf")
    for a in ASSETS:
        d = _haversine_km(lat, lon, a["lat"], a["lon"])
        if d < best_km:
            best, best_km = a, d
    return best, best_km


def _coverage(distance_km: float) -> float:
    """Linear coverage curve from 1.0 at 0 km → 0.0 at MAX_COVERAGE_KM."""
    if distance_km >= MAX_COVERAGE_KM:
        return 0.0
    return 1.0 - (distance_km / MAX_COVERAGE_KM)


def _near_incident(lon: float, lat: float, incidents: list[dict]) -> bool:
    """Skip cells already adjacent to a known active incident."""
    for i in incidents:
        i_lon, i_lat = i.get("lon"), i.get("lat")
        if i_lon is None or i_lat is None:
            continue
        if _haversine_km(lat, lon, i_lat, i_lon) < INCIDENT_DRAW_RADIUS_KM:
            return True
    return False


def recommend_prepositions(
    incidents: list[dict] | None,
    risk_grid: list[dict] | None,
) -> list[dict]:
    """Return up to STAGE_LIMIT preposition recommendations.

    Args:
      incidents:  list of NIFC active-incident dicts from nifc.py
      risk_grid:  list of grid-cell dicts from wildfire_risk.py

    Returns:
      List of {id, lon, lat, score, nearest_asset, rationale} dicts,
      sorted by score descending.
    """
    incidents = incidents or []
    risk_grid = risk_grid or []

    candidates: list[dict] = []
    for cell in risk_grid:
        risk = cell.get("risk_score") or 0.0
        if risk < STAGE_RISK_THRESHOLD:
            continue
        lon, lat = cell.get("lon"), cell.get("lat")
        if lon is None or lat is None:
            continue
        if _near_incident(lon, lat, incidents):
            continue

        asset, dist_km = _nearest_asset(lon, lat)
        if asset is None:
            continue
        coverage = _coverage(dist_km)
        uncovered = risk * (1.0 - coverage)

        # Stable id per (lon, lat) so the same cell keeps the same id
        # across refreshes when the operator is mid-decision.
        cell_key = f"{round(lon, 2)}_{round(lat, 2)}"
        cell_id = f"pre_{hashlib.sha256(cell_key.encode()).hexdigest()[:10]}"

        rationale = (
            f"HDW {cell.get('hdw', '?')} (risk {risk:.2f}); "
            f"peak conditions {cell.get('peak_temp_c', '?')}°C, "
            f"{cell.get('peak_rh_pct', '?')}% RH, "
            f"{cell.get('peak_wind_ms', '?')} m/s wind. "
            f"Nearest unit: {asset['name']} ({asset['type']}) "
            f"{dist_km:.0f} km away — coverage {coverage:.2f}. "
            f"Uncovered risk {uncovered:.2f}."
        )
        candidates.append({
            "id": cell_id,
            "lon": lon, "lat": lat,
            "score": round(uncovered, 3),
            "risk_score": risk,
            "hdw": cell.get("hdw"),
            "nearest_asset": {
                "name": asset["name"], "type": asset["type"],
                "lon": asset["lon"], "lat": asset["lat"],
                "distance_km": round(dist_km, 1),
                "coverage": round(coverage, 2),
            },
            "rationale": rationale,
        })

    candidates.sort(key=lambda c: -c["score"])
    return candidates[:STAGE_LIMIT]


def list_assets() -> list[dict]:
    """Asset catalog for the /wildfire/assets endpoint."""
    return list(ASSETS)
