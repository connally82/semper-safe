"""
Wildfire ignition-risk grid.

Computes a Hot-Dry-Windy index for a coarse grid covering the Western
US AOI, sourcing temperature, RH, and wind from the NWS Gridpoint
Forecast API (api.weather.gov/points/{lat},{lon} → forecastGridData).
Outputs a list of grid cells with normalized risk scores [0,1] that
the frontend renders as a heatmap.

The Hot-Dry-Windy index (Srock et al. 2018) is the most-cited fire
weather index that's computable from publicly available forecasts:

  HDW = max(over forecast hours) of (wind_speed_ms × VPD_hpa)
  VPD = SVP(T) − AVP(T, RH)        # vapor pressure deficit
  SVP = 6.112 × exp(17.67·Tc / (Tc + 243.5))   # saturation vapor pressure
  AVP = SVP × (RH/100)             # actual vapor pressure

Higher HDW = more fire-conducive (hot, dry, windy). Threshold zones:
  HDW < 200    → low
  200-400      → elevated
  400-600      → critical
  600+         → extreme (Paradise day was ~750)

We normalize to [0,1] for the frontend heatmap by dividing by 800
(slightly above the empirical max).

The NWS Gridpoint API is fast (<300 ms per cell) but rate-limited;
keep the grid coarse and the refresh cadence ≥5 min.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable

log = logging.getLogger("wildfire_risk")

# Match the wildfire AOI clamp.
AOI_LON_MIN, AOI_LON_MAX = -125.0, -100.0
AOI_LAT_MIN, AOI_LAT_MAX = 30.0, 50.0

# 2.5° grid → 50 cells (10 cols × 5 rows) covering the Western US.
# Each cell is sampled at its center via the NWS Gridpoint API.
GRID_STEP_DEG = 2.5

# HDW value at which the heatmap saturates (renders fully red).
HDW_SATURATION = 800.0

# User-Agent required by NWS API.
_USER_AGENT = "semper-safe/0.1 (https://sempersafe.live; ops@sempersafe.live)"


def _grid_centers() -> Iterable[tuple[float, float]]:
    """Yield (lon, lat) centers for each grid cell in the AOI."""
    lon = AOI_LON_MIN + GRID_STEP_DEG / 2
    while lon < AOI_LON_MAX:
        lat = AOI_LAT_MIN + GRID_STEP_DEG / 2
        while lat < AOI_LAT_MAX:
            yield round(lon, 3), round(lat, 3)
            lat += GRID_STEP_DEG
        lon += GRID_STEP_DEG


def _saturation_vapor_pressure_hpa(temp_c: float) -> float:
    """Magnus-form approximation. Returns SVP in hPa."""
    return 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))


def _hdw_index(temp_c: float, rh_pct: float, wind_ms: float) -> float:
    """Hot-Dry-Windy index for one forecast hour."""
    svp = _saturation_vapor_pressure_hpa(temp_c)
    avp = svp * max(0.0, min(100.0, rh_pct)) / 100.0
    vpd = max(0.0, svp - avp)
    return max(0.0, wind_ms) * vpd


def _sample_one_cell(client, lon: float, lat: float) -> dict | None:
    """Hit the NWS Gridpoint API for one lat/lon, return aggregated HDW.

    Two hops:
      1. /points/{lat},{lon} → returns the gridpoint URL
      2. that URL → returns forecast properties.temperature/
         relativeHumidity/windSpeed timeseries
    """
    try:
        r1 = client.get(f"https://api.weather.gov/points/{lat},{lon}")
        r1.raise_for_status()
        grid_url = (r1.json().get("properties") or {}).get("forecastGridData")
        if not grid_url:
            return None
        r2 = client.get(grid_url)
        r2.raise_for_status()
        props = (r2.json() or {}).get("properties") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("gridpoint fetch failed for (%s, %s): %s", lon, lat, exc)
        return None

    def _series(field: str) -> list[float]:
        block = props.get(field) or {}
        values = block.get("values") or []
        return [
            v.get("value") for v in values
            if isinstance(v.get("value"), (int, float))
        ][:24]   # cap at 24 forecast hours

    temps = _series("temperature")               # °C
    rhs = _series("relativeHumidity")            # %
    winds = _series("windSpeed")                  # km/h
    if not temps or not rhs or not winds:
        return None
    # Align lengths.
    n = min(len(temps), len(rhs), len(winds))
    temps, rhs, winds = temps[:n], rhs[:n], winds[:n]

    # Convert windSpeed km/h → m/s
    winds_ms = [w / 3.6 for w in winds]

    hdw_values = [_hdw_index(t, r, w) for t, r, w in zip(temps, rhs, winds_ms)]
    hdw_max = max(hdw_values) if hdw_values else 0.0
    # Pick the conditions at the peak hour — operator wants to know
    # WHAT made it bad, not just the score.
    peak_idx = hdw_values.index(hdw_max) if hdw_values else 0
    return {
        "lon": lon, "lat": lat,
        "hdw": round(hdw_max, 1),
        "risk_score": round(min(1.0, hdw_max / HDW_SATURATION), 3),
        "peak_temp_c":  round(temps[peak_idx], 1),
        "peak_rh_pct":  round(rhs[peak_idx], 1),
        "peak_wind_ms": round(winds_ms[peak_idx], 1),
    }


def compute_risk_grid() -> list[dict]:
    """Sample every grid cell and return the HDW index for each.

    Sampling all ~50 cells in series takes ~25 s at NWS API latency.
    Acceptable for a 5-min refresh loop. To speed up further: switch
    to a thread pool, or replace per-cell sampling with a single pull
    of the RTMA gridded dataset.
    """
    import httpx

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    grid: list[dict] = []
    with httpx.Client(timeout=15.0, headers=headers) as client:
        for lon, lat in _grid_centers():
            row = _sample_one_cell(client, lon, lat)
            if row is not None:
                grid.append(row)
    return grid
