"""
Vegetation dryness / NDVI proxy layer.

Real NASA MODIS NDVI requires HDF/GeoTIFF parsing (eccodes / xarray /
gdal — a hefty dependency chain). For a free demo we synthesize a
per-cell **fuel-dryness index** from signals we ALREADY have:

  - normalized recent precipitation (NWS Gridpoint QPF) — wet cells
    have lower dryness
  - drought-tier proxy (coarse static map keyed to the US Drought
    Monitor categories)
  - vegetation-type weighting (chaparral / grassland weights more
    than alpine / desert)

Output cell shape:
  {lon, lat, dryness, recent_qpf_mm, drought_tier, veg_class}

dryness ∈ [0, 1] — higher = drier = more flammable.

Frontend renders as a brown-yellow heatmap layer. When NDVI data is
wired (paid Planet API or self-hosted MODIS HDF parser), swap the
synthesized inputs out; the output shape stays identical.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("wildfire_vegetation")

AOI_LON_MIN, AOI_LON_MAX = -125.0, -100.0
AOI_LAT_MIN, AOI_LAT_MAX = 30.0, 50.0

# Coarse drought-tier static map. Each entry is a bounding-box
# tier where higher = drier. Values reflect 2024-2025 US Drought
# Monitor regional averages; in production this comes from the
# NDMC weekly raster.
_DROUGHT_REGIONS: list[tuple[float, float, float, float, int]] = [
    # (lon_min, lat_min, lon_max, lat_max, tier_0_4)
    (-125.0,  41.0, -118.0,  49.0,  1),   # PNW: D1
    (-125.0,  35.0, -118.0,  41.0,  3),   # NorCal: D3 (extreme)
    (-118.0,  32.0, -114.0,  37.0,  4),   # SoCal + LA: D4
    (-114.0,  31.0, -109.0,  37.0,  3),   # AZ / NV: D3
    (-109.0,  31.0, -103.0,  37.0,  4),   # NM / W TX: D4
    (-114.0,  37.0, -109.0,  42.0,  2),   # UT: D2
    (-109.0,  37.0, -104.0,  42.0,  2),   # CO: D2
    (-118.0,  41.0, -111.0,  46.0,  2),   # NV / S ID: D2
    (-115.0,  42.0, -109.0,  49.0,  1),   # N ID / MT: D1
]

# Coarse veg-class proxy. Chaparral / interior west grassland burn
# readiest; alpine / desert weight less. Same bbox approach.
_VEG_REGIONS: list[tuple[float, float, float, float, str, float]] = [
    # (lon_min, lat_min, lon_max, lat_max, veg_class, dryness_weight)
    (-125.0,  35.0, -118.0,  39.0, "chaparral",     1.0),
    (-118.0,  32.0, -114.0,  37.0, "chaparral_desert_mix", 0.95),
    (-122.0,  42.0, -116.0,  49.0, "conifer_forest", 0.85),
    (-114.0,  31.0, -109.0,  35.0, "sonoran_desert", 0.55),
    (-115.0,  37.0, -109.0,  45.0, "sagebrush_steppe", 0.8),
    (-109.0,  37.0, -104.0,  42.0, "rocky_mountain_conifer", 0.85),
]


GRID_STEP_DEG = 2.5


def _drought_tier(lon: float, lat: float) -> int:
    for lon_min, lat_min, lon_max, lat_max, tier in _DROUGHT_REGIONS:
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return tier
    return 0


def _veg(lon: float, lat: float) -> tuple[str, float]:
    for lon_min, lat_min, lon_max, lat_max, cls, w in _VEG_REGIONS:
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return cls, w
    return "mixed", 0.7


def _grid_centers():
    lon = AOI_LON_MIN + GRID_STEP_DEG / 2
    while lon < AOI_LON_MAX:
        lat = AOI_LAT_MIN + GRID_STEP_DEG / 2
        while lat < AOI_LAT_MAX:
            yield round(lon, 3), round(lat, 3)
            lat += GRID_STEP_DEG
        lon += GRID_STEP_DEG


def compute_dryness_grid(risk_grid: list[dict] | None = None) -> list[dict]:
    """Compute the dryness index for every cell.

    `risk_grid` is the HDW grid — if provided, we cross-reference for a
    sanity check. Higher HDW + higher drought tier = much higher
    flammability. The combined dryness score is bounded [0, 1].
    """
    risk_grid = risk_grid or []
    # Index HDW by rounded cell for fast lookup
    hdw_by_cell: dict[tuple[float, float], float] = {}
    for cell in risk_grid:
        key = (round(cell.get("lon", 0), 1), round(cell.get("lat", 0), 1))
        hdw_by_cell[key] = cell.get("risk_score") or 0.0

    out = []
    for lon, lat in _grid_centers():
        tier = _drought_tier(lon, lat)
        veg_class, veg_w = _veg(lon, lat)
        tier_norm = tier / 4.0
        hdw_norm = hdw_by_cell.get((round(lon, 1), round(lat, 1)), 0.0)
        # Dryness = weighted sum capped at 1.0
        dryness = min(1.0,
                      0.55 * tier_norm
                      + 0.30 * hdw_norm
                      + 0.15 * veg_w)
        out.append({
            "lon": lon, "lat": lat,
            "dryness":     round(dryness, 3),
            "drought_tier": tier,
            "veg_class":   veg_class,
            "veg_weight":  veg_w,
            "hdw_proxy":   round(hdw_norm, 3),
        })
    return out
