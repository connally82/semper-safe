"""
Lightning-strike feed for wildfire prevention.

Real-time cloud-to-ground (CG) lightning data costs money — Vaisala
NLDN, Earth Networks ENTLN, and Whether Tools API all require paid
subscriptions. For a public-feed demo we synthesize a realistic
strike pattern derived from NWS active Severe Thunderstorm and
Tornado warning polygons (where there's a thunderstorm, there are
strikes). Each synthesized strike carries:

  - lon, lat (jittered inside the warning polygon's bbox)
  - t (ISO 8601 within the last hour)
  - polarity ('+' or '-' CG)
  - amplitude_ka (peak current; 10-50 kA realistic range)
  - source ('synthesized' or 'nws_proxy' or future 'vaisala' / 'entln')

The interface is identical to what a paid feed would return, so
swapping in a real source is one config change.

When no thunderstorm warnings are active in the AOI, return [].
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wildfire_lightning")

AOI_LON_MIN, AOI_LON_MAX = -125.0, -100.0
AOI_LAT_MIN, AOI_LAT_MAX = 30.0, 50.0

_NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
_USER_AGENT = "semper-safe/0.1 (https://sempersafe.live; ops@sempersafe.live)"

_THUNDERSTORM_EVENTS = ["Severe Thunderstorm Warning"]

# Strikes per square degree per active warning. Realistic upper bound
# for a strong cell is ~200 strikes/hour over a small area. We cap so
# the frontend doesn't render thousands of dots.
STRIKES_PER_WARNING = 30
MAX_STRIKES_TOTAL = 400


def _bbox_of_geom(geom: dict) -> tuple[float, float, float, float] | None:
    """Return (lon_min, lat_min, lon_max, lat_max) for a Polygon /
    MultiPolygon GeoJSON geometry. Used to scatter synthesized strikes
    inside the warning area without doing point-in-polygon (good enough
    for the demo — real lightning data wouldn't need this)."""
    if not geom:
        return None
    coords = geom.get("coordinates")
    if not coords:
        return None
    t = geom.get("type", "")
    rings = []
    if t == "Polygon":
        rings = coords
    elif t == "MultiPolygon":
        for poly in coords:
            rings.extend(poly)
    else:
        return None
    lons: list[float] = []
    lats: list[float] = []
    for ring in rings:
        for pt in ring:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                lons.append(pt[0])
                lats.append(pt[1])
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


def _stable_seed(alert_id: str | None) -> int:
    """Deterministic per-warning seed so the same warning always
    produces the same synthetic strikes — strikes don't shuffle
    around the map on every refresh."""
    h = hashlib.sha256((alert_id or "").encode()).hexdigest()[:8]
    return int(h, 16)


def recent_strikes() -> list[dict]:
    """Return last-hour lightning strikes inside the AOI.

    Synthesized from active NWS thunderstorm-warning polygons; falls
    back to an empty list when no thunderstorm warnings are active.
    """
    import httpx

    out: list[dict] = []
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)

    for event in _THUNDERSTORM_EVENTS:
        try:
            with httpx.Client(timeout=20.0, headers=headers) as client:
                r = client.get(_NWS_ALERTS_URL, params={"event": event})
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("thunderstorm alert fetch failed: %s", exc)
            continue
        for feat in data.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            bbox = _bbox_of_geom(geom)
            if bbox is None:
                continue
            lon_min, lat_min, lon_max, lat_max = bbox
            # Clip to AOI.
            lon_min = max(lon_min, AOI_LON_MIN)
            lon_max = min(lon_max, AOI_LON_MAX)
            lat_min = max(lat_min, AOI_LAT_MIN)
            lat_max = min(lat_max, AOI_LAT_MAX)
            if lon_min >= lon_max or lat_min >= lat_max:
                continue
            props = feat.get("properties") or {}
            alert_id = feat.get("id") or props.get("id")
            rng = random.Random(_stable_seed(alert_id))
            for i in range(STRIKES_PER_WARNING):
                # Each strike has a random timestamp in the last hour.
                t_off = timedelta(seconds=rng.uniform(0, 3600))
                strike_t = now - t_off
                if strike_t < cutoff:
                    continue
                lon = rng.uniform(lon_min, lon_max)
                lat = rng.uniform(lat_min, lat_max)
                polarity = "+" if rng.random() < 0.1 else "-"  # ~10% +CG
                ka = round(rng.uniform(10, 50), 1)
                out.append({
                    "id":           f"strike_{alert_id}_{i}",
                    "lon":          round(lon, 4),
                    "lat":          round(lat, 4),
                    "t":            strike_t.isoformat(),
                    "polarity":     polarity,
                    "amplitude_ka": ka,
                    "source":       "nws_thunderstorm_proxy",
                    "alert_id":     alert_id,
                })
                if len(out) >= MAX_STRIKES_TOTAL:
                    return out
    return out
