"""
NOAA NDBC buoy real-time data fetcher (sensor enh #5 — real-time pivot).

Why this exists:
  Sentinel passes are 6-day cycles and AIS gives us vessel positions but
  nothing else. NOAA's National Data Buoy Center publishes live readings
  from moored ocean buoys around the US coast, updated every 30 minutes:
  wind speed/direction, wave height/period, water + air temperature,
  barometric pressure. For maritime ops this is direct, ground-truth
  weather context — does the vessel motion match the surface currents?
  Are conditions consistent with the AIS-reported speed?

Data:
  Format: fixed-width text per buoy at
    https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt
  Each file has ~45 days of half-hourly observations, newest first.
  Free, no auth, no rate limit beyond reasonable polite use.

Texas-shoreline AOI buoys (verified active 2026):
  42020 — Corpus Christi (~26.97 N, 96.69 W)
  42035 — Galveston      (~29.23 N, 94.41 W)
  42019 — Freeport       (deprecated; appears as 404 — drop from list)
  42002 — West Gulf      (~25.79 N, 93.66 W) — deeper, just east of AOI
  42039 — Pensacola SE   (~28.79 N, 86.01 W) — outside AOI, included
                                                for east-Gulf context

We keep the list in the AOI file rather than DB so adding stations is
git-tracked.

What this module does:
  - fetch_station(station_id) → dict with the most recent observation
    plus station metadata.
  - fetch_all() → list[dict] for the AOI station list.

Typical latency end-to-end: 30-90 seconds from observation timestamp
to availability on NDBC's HTTPS server.
"""

from __future__ import annotations

import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("ndbc")

NDBC_REALTIME2_URL = "https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt"

# Texas-shoreline AOI buoys + nearby Gulf ones for context.
# (id, lat, lon, name) — coordinates as published by NDBC station pages.
TEXAS_BUOYS = [
    ("42020", 26.968, -96.694, "Corpus Christi"),
    ("42035", 29.232, -94.413, "Galveston"),
    ("42002", 25.790, -93.666, "West Gulf"),
    ("42039", 28.788, -86.008, "Pensacola SE"),
    ("BURL1", 28.905, -89.428, "Southwest Pass, LA"),
    ("PTAT2", 27.829, -97.050, "Port Aransas, TX"),
    ("EPTT2", 26.060, -97.181, "South Padre Island, TX"),
]


def _parse_float(s: str) -> float | None:
    if not s or s in ("MM", "999.0", "9999.0"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ms_to_kn(v: float | None) -> float | None:
    return None if v is None else v * 1.94384


def _parse_realtime2(text: str) -> dict[str, Any] | None:
    """Parse a NDBC realtime2 text blob — header + first data row.

    File layout:
      line 1: column names      — '#YY  MM DD ... WTMP DEWP VIS  PTDY  TIDE'
      line 2: column units      — '#yr  mo dy ... degC degC nmi  hPa   ft'
      line 3+: rows newest first

    Returns None if the file is malformed or has no data.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    header = lines[0].lstrip("#").split()
    # First non-comment row.
    for row in lines[2:]:
        if row.startswith("#"):
            continue
        fields = row.split()
        if len(fields) < 5:
            continue
        d = dict(zip(header, fields))
        try:
            t = datetime(
                int(d["YY"]), int(d["MM"]), int(d["DD"]),
                int(d["hh"]), int(d["mm"]),
                tzinfo=timezone.utc,
            )
        except (KeyError, ValueError):
            return None
        wspd = _parse_float(d.get("WSPD", ""))
        gst  = _parse_float(d.get("GST", ""))
        return {
            "t": t.isoformat(),
            "wind_dir_deg":  _parse_float(d.get("WDIR", "")),
            "wind_speed_kn": _ms_to_kn(wspd),
            "wind_gust_kn":  _ms_to_kn(gst),
            "wave_height_m": _parse_float(d.get("WVHT", "")),
            "dom_period_s":  _parse_float(d.get("DPD", "")),
            "avg_period_s":  _parse_float(d.get("APD", "")),
            "wave_dir_deg":  _parse_float(d.get("MWD", "")),
            "pressure_hpa":  _parse_float(d.get("PRES", "")),
            "air_temp_c":    _parse_float(d.get("ATMP", "")),
            "water_temp_c":  _parse_float(d.get("WTMP", "")),
            "dewpoint_c":    _parse_float(d.get("DEWP", "")),
            "visibility_nm": _parse_float(d.get("VIS", "")),
            "pressure_tendency_hpa": _parse_float(d.get("PTDY", "")),
            "tide_ft":       _parse_float(d.get("TIDE", "")),
        }
    return None


def fetch_station(station_id: str, *, timeout_s: float = 15.0
                  ) -> dict[str, Any] | None:
    """Pull realtime2 for one station, return the latest observation.

    Returns None on 404 or parse failure (NDBC drops stations
    occasionally — we want the listing endpoint to skip rather than
    error).
    """
    url = NDBC_REALTIME2_URL.format(sid=station_id)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Semper-Safe/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            text = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info("NDBC station %s: 404 (likely deprecated)", station_id)
            return None
        log.warning("NDBC station %s: HTTP %d", station_id, e.code)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("NDBC station %s: %s", station_id, e)
        return None
    return _parse_realtime2(text)


def fetch_all(stations: list[tuple[str, float, float, str]] = TEXAS_BUOYS,
              ) -> list[dict[str, Any]]:
    """Fetch latest observation for every station in the list.

    Returns a list of dicts shaped for the GeoJSON-style endpoint:
      { id, name, lat, lon, observation: {...} | null }

    Stations that 404 or fail parsing get observation=None so the
    frontend can render a "telemetry lost" marker instead of dropping
    the buoy entirely.
    """
    out = []
    for sid, lat, lon, name in stations:
        obs = fetch_station(sid)
        out.append({
            "station_id": sid,
            "name": name,
            "lat": lat,
            "lon": lon,
            "observation": obs,
        })
    return out
