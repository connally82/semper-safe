"""
Land mask — drop CFAR detections that fall on dry land.

Why this exists:
  Sentinel-1 IW GRDH scenes cover ~250×200 km swaths that span both
  ocean and continent. CFAR makes no distinction between water and
  land, so any bright surface return — buildings, agricultural patterns,
  surface roads, even forested patches with strong dihedral scattering —
  passes as a "vessel". Without a land mask, every Texas-shoreline scene
  produced 100s of inland false positives that drowned out the real
  Gulf vessels we care about.

What this module does:
  - Loads a Texas-Louisiana state-boundary multipolygon (from OSM, via
    backend/data/aoi_land.geojson) at first call.
  - Exposes is_on_land(lat, lon) — a single Shapely contains() check.
  - Acts as a lightweight prefilter inside sar_processor.process_scene
    so on-land detections never get persisted to sar_detections.

Source + accuracy:
  - OSM relations 114690 (Texas) + 224922 (Louisiana), fused via
    Overpass + Shapely unary_union, simplified to 0.001° tolerance
    (~100 m at this latitude). 4,631 polygon vertices.
  - State boundaries are slightly aggressive — Galveston Bay, Aransas
    Pass, and other coastal bays are inside Texas's legal boundary so
    they classify as "land" here. We accept the small false-negative
    rate (real vessels in shallow bays might be filtered) in exchange
    for the much larger false-positive reduction inland.
  - Phase 5 follow-up: replace with a real OSM `natural=coastline`
    polygon to recover bay coverage, or use NOAA NCEI bathymetry
    (depth > 0 = water).

Performance:
  - shapely.contains() on a 4,631-vertex polygon: ~50 μs per point.
  - 200 detections per scene → ~10 ms total — negligible vs CFAR.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger("land_mask")

DATA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "aoi_land.geojson",
)


@lru_cache(maxsize=1)
def _land_polygon():
    """Lazily load the AOI land multipolygon. Returns None if the file
    is missing — callers treat None as "mask disabled, never drop"."""
    if not os.path.exists(DATA_FILE):
        log.warning("land mask file missing: %s — land suppression disabled",
                    DATA_FILE)
        return None
    # Lazy shapely import: it's already a backend dep but importing it
    # at module load every time costs a few ms across the cold path.
    from shapely.geometry import shape

    with open(DATA_FILE) as f:
        feature = json.load(f)
    poly = shape(feature["geometry"])
    if not poly.is_valid:
        # Shapely's buffer(0) trick repairs self-intersections silently.
        # We've seen this on simplified state polygons where the
        # tolerance crosses a near-tangent boundary.
        log.warning("land polygon is not topologically valid; "
                    "running buffer(0) to repair")
        poly = poly.buffer(0)
    n_verts = (
        len(list(poly.exterior.coords))
        if hasattr(poly, "exterior")
        else sum(len(p.exterior.coords) for p in poly.geoms)
    )
    log.info("loaded land mask: %d vertices, area=%.2f sq-deg",
             n_verts, poly.area)
    return poly


def is_on_land(lat: float, lon: float) -> bool:
    """True if (lat, lon) is inside the Texas/Louisiana land polygon.

    Returns False if the mask file is missing — fail-open so a missing
    asset doesn't silently drop every detection. Verified by the
    sar_processor log line "platform suppression: N known structures",
    paired with "land mask: N polygons" at the same boot.
    """
    poly = _land_polygon()
    if poly is None:
        return False
    from shapely.geometry import Point
    return poly.contains(Point(lon, lat))


def is_loaded() -> bool:
    return _land_polygon() is not None
