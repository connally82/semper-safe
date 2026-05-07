"""
Synthetic wildfire scenario.

A 6-hour operational picture spanning Northern California with five
distinct events, designed to exercise every fusion path:

  1. Confirmed fire near WUI:    persistent hotspots + smoke + red flag
                                 conditions → EVACUATION_ADVISORY
  2. Confirmed remote fire:      persistent hotspots + smoke, no WUI
                                 → ALERT_FIRE_DISPATCH
  3. Industrial false positive:  hotspot inside known refinery flare
                                 buffer → suppressed before reaching ops
  4. Single-pixel hotspot:       isolated VIIRS pixel, no smoke, no
                                 persistence → low-priority watchlist
                                 (REQUEST_AERIAL_RECON)
  5. Orphan smoke plume:         optical detection without thermal
                                 anomaly → standalone SMOKE_PLUME

A working fusion engine should produce exactly that distribution.
"""

from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone

from models import Observation, SourceType, Geom


SCENARIO_START = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)


def _obs(source: SourceType, source_id: str, lon: float, lat: float,
         t: datetime, attrs: dict | None = None, conf: float = 1.0
         ) -> Observation:
    return Observation(
        obs_id=f"obs_{uuid.uuid4().hex[:12]}",
        source=source, source_id=source_id,
        geom=Geom(lon=lon, lat=lat),
        h3_cell=f"h3_{round(lat, 2)}_{round(lon, 2)}",
        t=t, attrs=attrs or {}, confidence=conf,
    )


def build_wildfire_scenario() -> list[Observation]:
    obs: list[Observation] = []

    # --- Event 1: Fire near Santa Rosa WUI ----------------------------
    # 4 VIIRS overpasses + 2 GOES detections + 1 smoke plume + weather
    fire1_lon, fire1_lat = -122.610, 38.475
    for i, dt in enumerate([0, 25, 50, 75]):
        obs.append(_obs(
            SourceType.VIIRS, f"viirs_pass_{i+1:02d}",
            fire1_lon + 0.001 * i, fire1_lat + 0.0008 * i,
            SCENARIO_START + timedelta(minutes=dt),
            attrs={"frp_mw": 14 + i * 5, "pixel_size": 375,
                   "brightness_t_k": 340 + i * 6},
            conf=0.78,
        ))
    # GOES sees it on 5-min cadence (we sample 2)
    for dt in [30, 60]:
        obs.append(_obs(
            SourceType.GOES, "goes_18_abi",
            fire1_lon + 0.002, fire1_lat + 0.001,
            SCENARIO_START + timedelta(minutes=dt),
            attrs={"frp_mw": 22, "pixel_size": 2000},
            conf=0.65,
        ))
    # Optical smoke plume
    obs.append(_obs(
        SourceType.OPTICAL, "sentinel2_l2a",
        fire1_lon + 0.015, fire1_lat + 0.012,
        SCENARIO_START + timedelta(minutes=45),
        attrs={"plume_length_km": 3.4, "plume_direction_deg": 285},
        conf=0.85,
    ))
    # Weather: red flag conditions
    obs.append(_obs(
        SourceType.WEATHER, "noaa_hrrr",
        fire1_lon, fire1_lat,
        SCENARIO_START + timedelta(minutes=80),
        attrs={"rh_pct": 12, "wind_mph": 31, "wind_gust_mph": 48,
               "temp_f": 92, "fuel_moisture": 6,
               "advisory": "RED FLAG WARNING"},
        conf=0.99,
    ))

    # --- Event 2: Remote fire (Trinity NF) ----------------------------
    fire2_lon, fire2_lat = -123.180, 40.610
    for i, dt in enumerate([10, 40, 70, 110]):
        obs.append(_obs(
            SourceType.VIIRS, f"viirs_pass_{i+1:02d}_r2",
            fire2_lon + 0.0008 * i, fire2_lat - 0.0006 * i,
            SCENARIO_START + timedelta(minutes=dt),
            attrs={"frp_mw": 8 + i * 3, "pixel_size": 375,
                   "brightness_t_k": 325 + i * 4},
            conf=0.74,
        ))
    obs.append(_obs(
        SourceType.OPTICAL, "sentinel2_l2a",
        fire2_lon + 0.008, fire2_lat - 0.005,
        SCENARIO_START + timedelta(minutes=85),
        attrs={"plume_length_km": 1.2, "plume_direction_deg": 145},
        conf=0.80,
    ))
    obs.append(_obs(
        SourceType.WEATHER, "noaa_hrrr",
        fire2_lon, fire2_lat,
        SCENARIO_START + timedelta(minutes=90),
        attrs={"rh_pct": 28, "wind_mph": 12, "fuel_moisture": 11,
               "temp_f": 78},
    ))

    # --- Event 3: Industrial false positive (Martinez refinery) -------
    obs.append(_obs(
        SourceType.VIIRS, "viirs_pass_03",
        -121.890, 38.020,
        SCENARIO_START + timedelta(minutes=55),
        attrs={"frp_mw": 31, "pixel_size": 375,
               "brightness_t_k": 365},
        conf=0.78,
    ))
    obs.append(_obs(
        SourceType.GOES, "goes_18_abi",
        -121.889, 38.021,
        SCENARIO_START + timedelta(minutes=60),
        attrs={"frp_mw": 28, "pixel_size": 2000},
        conf=0.65,
    ))

    # --- Event 4: Isolated single-pixel hotspot (likely glint) --------
    obs.append(_obs(
        SourceType.VIIRS, "viirs_pass_02",
        -120.420, 39.180,
        SCENARIO_START + timedelta(minutes=30),
        attrs={"frp_mw": 4, "pixel_size": 375,
               "brightness_t_k": 312,
               "note": "low FRP, single detection"},
        conf=0.55,
    ))

    # --- Event 5: Orphan smoke plume (no thermal) ---------------------
    obs.append(_obs(
        SourceType.OPTICAL, "sentinel2_l2a",
        -119.840, 36.720,
        SCENARIO_START + timedelta(minutes=70),
        attrs={"plume_length_km": 0.8, "plume_direction_deg": 90,
               "note": "possibly controlled burn, dust, or distant fire"},
        conf=0.55,
    ))

    obs.sort(key=lambda o: o.t)
    return obs
