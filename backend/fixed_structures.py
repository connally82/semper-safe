"""
Fixed offshore-structure registry — known-platform false-positive
suppression for the SAR pipeline (Phase 4.x).

Why this exists:
  CFAR will flag every oil rig + production platform + manned compliant
  buoy as a "vessel" because they reflect strongly + persist across
  passes. The Texas-Louisiana shelf alone has thousands of these
  (currently 1,432 in OSM within our extended AOI). Without
  suppression, ~10–30% of dark-vessel candidates from any
  deep-water Gulf pass would be these structures, drowning out real
  non-cooperative targets.

What this module does:
  - Loads the bundled OSM offshore-platform list at import time.
  - Exposes nearest_platform_m(lat, lon) — haversine distance to the
    closest known platform, in meters.
  - Exposes is_near_fixed_structure(lat, lon, radius_m) — a single
    bool used by sar_processor.process_scene to drop / tag detections
    that fall within radius_m of any known structure.

Data source:
  backend/data/gulf_offshore_platforms.json — OSM Overpass query
  for man_made=offshore_platform + seamark:type=offshore_platform
  inside (25.0, -98.5, 30.5, -91.0). Refresh by re-running the
  fetch script (`scripts/refresh_offshore_platforms.py` — TBD) when
  OSM updates significantly. ~42 KB on disk; loads in ~5 ms at boot.

Limitations (Phase 5 work):
  - OSM coverage is incomplete near recently decommissioned or new
    platforms. BSEE's authoritative GoM Platform table would replace
    this in production.
  - Buoys and aids-to-navigation (USCG ATON) are NOT in this list.
    Add a separate USCG ATON layer.
  - Fixed-radius suppression: simple but blunt. A high-confidence
    detection that's within 200 m of a platform is currently dropped;
    a real vessel docked at a platform supply boat would be missed.
    Phase 5 layer would consult vessel motion across passes.
"""

from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache

log = logging.getLogger("fixed_structures")

DATA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "gulf_offshore_platforms.json",
)


@lru_cache(maxsize=1)
def _platforms() -> list[tuple[float, float]]:
    """Load (lat, lon) tuples once. Cached for the process lifetime."""
    if not os.path.exists(DATA_FILE):
        log.warning("fixed-structure data file missing: %s — suppression disabled",
                    DATA_FILE)
        return []
    with open(DATA_FILE) as f:
        rows = json.load(f)
    # JSON entries are [lat, lon, name?]; drop name to keep the inner
    # loop allocation-free.
    pts = [(float(r[0]), float(r[1])) for r in rows]
    log.info("loaded %d known offshore platforms from %s", len(pts), DATA_FILE)
    return pts


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two (lat, lon) points."""
    R = 6_371_000.0
    a = math.radians(lat1)
    b = math.radians(lat2)
    dlat = b - a
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)
    return 2.0 * R * math.asin(math.sqrt(h))


def nearest_platform_m(lat: float, lon: float) -> float:
    """Distance in meters to the nearest known offshore platform.

    Returns +inf if the registry is empty (data file missing). Linear
    scan over all platforms — currently ~1.4 k entries, ~0.5 ms per
    detection. Fast enough that sar_processor.process_scene calls this
    inline; if the registry grows past ~100 k we'd switch to PostGIS
    ST_DWithin or an in-memory R-tree (rtree pkg).
    """
    pts = _platforms()
    if not pts:
        return float("inf")
    best = float("inf")
    for plat, plon in pts:
        d = _haversine_m(lat, lon, plat, plon)
        if d < best:
            best = d
    return best


def is_near_fixed_structure(lat: float, lon: float,
                             radius_m: float = 200.0) -> bool:
    """True if any known structure is within radius_m of (lat, lon).

    Default 200 m is comfortably inside the typical Sentinel-1 IW GRDH
    pixel footprint (10 m) and accounts for both GCP geocoding error
    (~30 m residual) and the spatial extent of multi-platform clusters.
    """
    return nearest_platform_m(lat, lon) <= radius_m


def platform_count() -> int:
    """Operational visibility — how many platforms are loaded."""
    return len(_platforms())
