"""
Synthetic scenario generator.

Builds a deterministic 6-hour operational picture of a 200x200 nm patch of
ocean with three classes of activity:

  - 12 normal vessels reporting AIS continuously (noise floor)
  - 1  vessel that legitimately goes dark (entering port for maintenance)
  - 2  vessels that go dark in a marine protected area (the IUU pattern)
  - 4  SAR satellite passes detecting the IUU vessels

A working fusion engine should:
  - Treat the 12 normal vessels as routine (low priority)
  - Flag the legitimate dropout as an AIS gap, but not escalate
  - Flag the 2 IUU vessels as DARK_VESSEL and recommend tasking
"""

from __future__ import annotations
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from models import Observation, SourceType, Geom


SCENARIO_START = datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)

# Synthetic ocean patch — somewhere off the coast of NW Madagascar
# (no actual operational meaning; deterministic for the demo)
LON_MIN, LON_MAX = 47.5, 49.0
LAT_MIN, LAT_MAX = -14.5, -13.0

# A made-up "MPA" inside the patch — IUU activity will cluster here
MPA_LON, MPA_LAT, MPA_RADIUS_DEG = 48.2, -13.7, 0.25


def _h3_stub(lon: float, lat: float) -> str:
    """Stand-in for an H3 index. Real impl: h3.geo_to_h3(lat, lon, 8)."""
    return f"h3_{round(lat, 2)}_{round(lon, 2)}"


def _obs(source: SourceType, source_id: str, lon: float, lat: float,
         t: datetime, attrs: dict | None = None, conf: float = 1.0) -> Observation:
    return Observation(
        obs_id=f"obs_{uuid.uuid4().hex[:12]}",
        source=source,
        source_id=source_id,
        geom=Geom(lon=lon, lat=lat),
        h3_cell=_h3_stub(lon, lat),
        t=t,
        attrs=attrs or {},
        confidence=conf,
    )


def _track(start_lon: float, start_lat: float,
           heading_deg: float, speed_kn: float,
           start_t: datetime, end_t: datetime, step: timedelta):
    """Yield (t, lon, lat) along a great-circle approximation."""
    # Crude flat-earth approx is fine inside a 200nm patch
    speed_deg_per_min = speed_kn / 60 / 60   # rough conversion
    rad = math.radians(heading_deg)
    t = start_t
    lon, lat = start_lon, start_lat
    while t <= end_t:
        yield t, lon, lat
        dt_min = step.total_seconds() / 60
        lon += math.sin(rad) * speed_deg_per_min * dt_min
        lat += math.cos(rad) * speed_deg_per_min * dt_min
        t += step


def build_scenario() -> list[Observation]:
    rng = random.Random(20260507)
    obs: list[Observation] = []

    # --- 12 normal vessels: AIS every 3 minutes, full duration -------
    for i in range(12):
        mmsi = f"3{rng.randint(10000000, 99999999)}"
        lon0 = rng.uniform(LON_MIN, LON_MAX)
        lat0 = rng.uniform(LAT_MIN, LAT_MAX)
        heading = rng.uniform(0, 360)
        speed = rng.uniform(8, 14)
        vname = rng.choice([
            "MV ALBATROSS", "MV NORDIC SUN", "MV TRADEWIND", "MV KESTREL",
            "MV SOUTHERN CROSS", "MV CALYPSO", "MV PETREL", "MV SEA PIONEER",
            "MV BLUEFIN", "MV HORIZON", "MV TRITON", "MV MARLIN",
        ])
        for t, lon, lat in _track(lon0, lat0, heading, speed,
                                   SCENARIO_START,
                                   SCENARIO_START + timedelta(hours=6),
                                   timedelta(minutes=3)):
            obs.append(_obs(SourceType.AIS, mmsi, lon, lat, t,
                            attrs={"name": vname, "type": "cargo",
                                   "heading": heading, "speed_kn": speed}))

    # --- 1 legitimate dropout: vessel reports it's entering port ------
    legit_mmsi = "311234567"
    lon0 = LON_MAX - 0.1
    lat0 = LAT_MIN + 0.2
    for t, lon, lat in _track(lon0, lat0, 270, 10,
                               SCENARIO_START,
                               SCENARIO_START + timedelta(hours=2),
                               timedelta(minutes=3)):
        attrs = {"name": "MV CORAL VOYAGER", "type": "cargo",
                 "heading": 270, "speed_kn": 10}
        if t >= SCENARIO_START + timedelta(hours=1, minutes=55):
            attrs["nav_status"] = "moored / port maintenance scheduled"
        obs.append(_obs(SourceType.AIS, legit_mmsi, lon, lat, t, attrs=attrs))
    # ...then no further AIS reports. Self-declared maintenance.

    # --- 2 IUU vessels: report normally, then go dark inside MPA -----
    iuu_specs = [
        {"mmsi": "412345678", "name": "FV SEA TIGER",
         "go_dark_at": SCENARIO_START + timedelta(hours=2, minutes=30),
         "approach_from": (LON_MIN + 0.2, LAT_MAX - 0.1, 135, 11)},
        {"mmsi": "413456789", "name": "FV OCEAN HARVEST",
         "go_dark_at": SCENARIO_START + timedelta(hours=3, minutes=10),
         "approach_from": (LON_MAX - 0.3, LAT_MIN + 0.1, 315, 9)},
    ]
    iuu_dark_positions: list[tuple[str, float, float, datetime]] = []
    for spec in iuu_specs:
        lon0, lat0, heading, speed = spec["approach_from"]
        last_lon, last_lat = lon0, lat0
        for t, lon, lat in _track(lon0, lat0, heading, speed,
                                   SCENARIO_START,
                                   spec["go_dark_at"],
                                   timedelta(minutes=3)):
            obs.append(_obs(SourceType.AIS, spec["mmsi"], lon, lat, t,
                            attrs={"name": spec["name"], "type": "fishing",
                                   "heading": heading, "speed_kn": speed}))
            last_lon, last_lat = lon, lat
            # Note: last_t was tracked here in earlier iterations but is unused
            # downstream — go-dark drift uses spec["go_dark_at"] directly.
        # Estimate where they actually went after going dark
        # (drifting toward MPA at reduced speed)
        for minutes_after in range(15, 180, 30):
            t_drift = spec["go_dark_at"] + timedelta(minutes=minutes_after)
            # Drift toward MPA center
            dlon = MPA_LON - last_lon
            dlat = MPA_LAT - last_lat
            mag = math.hypot(dlon, dlat) or 1
            drift_lon = last_lon + (dlon / mag) * 0.05 * (minutes_after / 30)
            drift_lat = last_lat + (dlat / mag) * 0.05 * (minutes_after / 30)
            iuu_dark_positions.append((spec["mmsi"], drift_lon, drift_lat, t_drift))

    # --- 4 SAR satellite passes ---------------------------------------
    # Each pass is a moment in time; detections are the dark vessels visible
    # in that pass's footprint (we treat the whole patch as one footprint).
    for pass_num, offset in enumerate([
        timedelta(hours=3),
        timedelta(hours=3, minutes=45),
        timedelta(hours=4, minutes=30),
        timedelta(hours=5, minutes=15),
    ]):
        pass_t = SCENARIO_START + offset
        pass_id = f"capella_pass_{pass_num+1:03d}"
        # SAR also "sees" all the cooperative vessels — these will fuse cleanly
        for o in obs:
            if o.source != SourceType.AIS:
                continue
            if abs((o.t - pass_t).total_seconds()) > 90:
                continue
            # Add SAR observation at same location (small noise)
            obs.append(_obs(
                SourceType.SAR, pass_id,
                o.geom.lon + rng.uniform(-0.005, 0.005),
                o.geom.lat + rng.uniform(-0.005, 0.005),
                pass_t,
                attrs={"length_m": rng.randint(80, 220),
                       "rcs_db": rng.uniform(15, 30)},
                conf=0.85,
            ))

        # And the dark vessels, at their drifted positions
        for mmsi, dlon, dlat, dt in iuu_dark_positions:
            if abs((dt - pass_t).total_seconds()) > 900:  # within 15 min of pass
                continue
            obs.append(_obs(
                SourceType.SAR, pass_id,
                dlon + rng.uniform(-0.003, 0.003),
                dlat + rng.uniform(-0.003, 0.003),
                pass_t,
                attrs={"length_m": rng.randint(35, 65),  # smaller — fishing vessel
                       "rcs_db": rng.uniform(8, 15)},
                conf=0.78,
            ))

    obs.sort(key=lambda o: o.t)
    return obs
