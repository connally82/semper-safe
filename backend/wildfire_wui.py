"""
Wildland-Urban Interface (WUI) communities — risk-scoring catalog.

Curated list of major Western US WUI communities (CA, OR, WA, ID, MT,
CO, NM, AZ, UT, NV) drawn from USFS WUI mapping + CAL FIRE FRAP
Communities-At-Risk + post-2010 destructive-fire history.

Each entry carries:
  - name, state, county
  - lon / lat (community center)
  - population (rounded thousands)
  - structures (rounded hundreds)
  - history (recent destructive fires touching the community)

`score_community(wui, risk_grid)` ranks each community by
  exposure = population_norm × structure_density × max_nearby_HDW,
where max_nearby_HDW is the highest risk_score in the risk grid
within WUI_RISK_RADIUS_KM of the community center.

For the demo, communities are scored against the live HDW grid each
refresh tick. Top-N (by score) get rendered prominently on the
frontend with a tiered color: green / amber / red / deep-red.
"""

from __future__ import annotations

import math
from typing import Iterable


WUI_RISK_RADIUS_KM = 50.0


# (name, state, county, lon, lat, population, structures, history)
WUI_COMMUNITIES: list[dict] = [
    # ── California ────────────────────────────────────────
    {"name": "Paradise",        "state": "CA", "county": "Butte",
     "lon": -121.62, "lat": 39.76, "pop": 27, "structures": 11500,
     "history": ["Camp Fire 2018"]},
    {"name": "Santa Rosa",      "state": "CA", "county": "Sonoma",
     "lon": -122.71, "lat": 38.44, "pop": 178, "structures": 76000,
     "history": ["Tubbs 2017", "Glass 2020"]},
    {"name": "Lake County",     "state": "CA", "county": "Lake",
     "lon": -122.91, "lat": 39.10, "pop": 64, "structures": 32000,
     "history": ["Mendocino Complex 2018", "LNU Lightning 2020"]},
    {"name": "Malibu",          "state": "CA", "county": "Los Angeles",
     "lon": -118.78, "lat": 34.04, "pop": 11, "structures": 6200,
     "history": ["Woolsey 2018"]},
    {"name": "Idyllwild",       "state": "CA", "county": "Riverside",
     "lon": -116.71, "lat": 33.74, "pop": 4, "structures": 2400,
     "history": ["Mountain Fire 2013"]},
    {"name": "Big Bear",        "state": "CA", "county": "San Bernardino",
     "lon": -116.91, "lat": 34.24, "pop": 12, "structures": 11200,
     "history": ["El Dorado 2020 evac"]},
    {"name": "Julian",          "state": "CA", "county": "San Diego",
     "lon": -116.60, "lat": 33.08, "pop": 2, "structures": 1200,
     "history": ["Cedar 2003"]},
    {"name": "Mariposa",        "state": "CA", "county": "Mariposa",
     "lon": -119.97, "lat": 37.48, "pop": 16, "structures": 8800,
     "history": ["Detwiler 2017", "Ferguson 2018"]},
    # ── Oregon / Washington ────────────────────────────────
    {"name": "Talent / Phoenix","state": "OR", "county": "Jackson",
     "lon": -122.78, "lat": 42.25, "pop": 11, "structures": 4400,
     "history": ["Almeda 2020"]},
    {"name": "Estacada",        "state": "OR", "county": "Clackamas",
     "lon": -122.34, "lat": 45.29, "pop": 4, "structures": 1800,
     "history": ["Riverside 2020"]},
    {"name": "Bend",            "state": "OR", "county": "Deschutes",
     "lon": -121.31, "lat": 44.06, "pop": 100, "structures": 42000,
     "history": ["Two Bulls 2014 evac"]},
    {"name": "Yakima",          "state": "WA", "county": "Yakima",
     "lon": -120.51, "lat": 46.60, "pop": 95, "structures": 36000,
     "history": ["Evans Canyon 2020"]},
    {"name": "Wenatchee",       "state": "WA", "county": "Chelan",
     "lon": -120.32, "lat": 47.42, "pop": 35, "structures": 14000,
     "history": ["Sleepy Hollow 2015"]},
    # ── Rockies / Southwest ────────────────────────────────
    {"name": "Boulder",         "state": "CO", "county": "Boulder",
     "lon": -105.27, "lat": 40.01, "pop": 104, "structures": 45000,
     "history": ["Marshall 2021"]},
    {"name": "Estes Park",      "state": "CO", "county": "Larimer",
     "lon": -105.52, "lat": 40.38, "pop": 6, "structures": 5800,
     "history": ["East Troublesome 2020 evac"]},
    {"name": "Ruidoso",         "state": "NM", "county": "Lincoln",
     "lon": -105.67, "lat": 33.33, "pop": 7, "structures": 8400,
     "history": ["Little Bear 2012", "South Fork 2024"]},
    {"name": "Flagstaff",       "state": "AZ", "county": "Coconino",
     "lon": -111.65, "lat": 35.20, "pop": 76, "structures": 33000,
     "history": ["Schultz 2010", "Pipeline 2022", "Tunnel 2022"]},
    {"name": "Prescott",        "state": "AZ", "county": "Yavapai",
     "lon": -112.47, "lat": 34.54, "pop": 47, "structures": 21000,
     "history": ["Doce 2013", "Yarnell Hill 2013 (next-town)"]},
    {"name": "Park City",       "state": "UT", "county": "Summit",
     "lon": -111.50, "lat": 40.65, "pop": 9, "structures": 11000,
     "history": ["Parleys 2022 evac"]},
    {"name": "Reno-Sparks",     "state": "NV", "county": "Washoe",
     "lon": -119.76, "lat": 39.53, "pop": 273, "structures": 110000,
     "history": ["Caughlin 2011"]},
    {"name": "Lake Tahoe South","state": "CA", "county": "El Dorado",
     "lon": -119.97, "lat": 38.93, "pop": 22, "structures": 13000,
     "history": ["Caldor 2021 (next-town)"]},
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def score_communities(risk_grid: list[dict] | None) -> list[dict]:
    """Return WUI communities ranked by today's exposure.

    For each community: find max HDW risk_score within WUI_RISK_RADIUS_KM
    of the center, normalize population, multiply by structure
    density proxy (structures / pop), and rank.
    """
    risk_grid = risk_grid or []
    out: list[dict] = []
    max_pop = max((c["pop"] for c in WUI_COMMUNITIES), default=1)
    for c in WUI_COMMUNITIES:
        # max nearby HDW
        max_risk = 0.0
        nearest_dist = float("inf")
        for cell in risk_grid:
            r = cell.get("risk_score") or 0.0
            d = _haversine_km(c["lat"], c["lon"], cell["lat"], cell["lon"])
            if d <= WUI_RISK_RADIUS_KM and r > max_risk:
                max_risk = r
            if d < nearest_dist:
                nearest_dist = d
        pop_norm = c["pop"] / max_pop
        struct_density = (c["structures"] / max(c["pop"], 1)) / 100.0   # rough
        struct_density = min(struct_density, 1.0)
        score = round(pop_norm * struct_density * max_risk, 3)
        # Tier label for the frontend
        if max_risk >= 0.75: tier = "extreme"
        elif max_risk >= 0.5: tier = "high"
        elif max_risk >= 0.25: tier = "elevated"
        else: tier = "normal"
        out.append({
            **c,
            "score": score,
            "max_nearby_hdw_risk": round(max_risk, 3),
            "tier": tier,
        })
    out.sort(key=lambda c: -c["score"])
    return out
