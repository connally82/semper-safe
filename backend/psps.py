"""
PSPS (Public Safety Power Shutoff) zone catalog.

California IOUs — PG&E, SCE, SDG&E, BVES — issue PSPS de-energizations
when fire-weather conditions threaten power-line ignitions. PG&E
publishes active events on a public web app but no clean API;
SCE has an ArcGIS feature service intermittently. For a demo we
ship a curated catalog of representative zones, with the same data
shape a real feed would return.

Each entry:
  zone_id (utility-specific),
  utility (PG&E / SCE / SDG&E / BVES),
  name (community / area),
  status ('active' | 'scheduled' | 'standby'),
  starts_at, ends_at (ISO 8601),
  customers_affected (estimated),
  geometry (GeoJSON Polygon — county / district outline),
  reason (free-form NWS-driven justification).

When the user wires a real utility feed (or a paid wildfiretoolkit),
swap KNOWN_PSPS_ZONES out for the live result. Endpoint shape stays
identical so the frontend doesn't change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _isofuture(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _isopast(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# Representative PSPS zones for demo. Coordinates are bbox polygons
# approximating real PG&E / SCE / SDG&E shutoff regions from
# historical events (2019-2024). When a real feed lands these get
# replaced wholesale.
KNOWN_PSPS_ZONES: list[dict] = [
    {
        "zone_id": "pge_butte_240507",
        "utility": "PG&E",
        "name": "Butte / Plumas — Paradise corridor",
        "status": "active",
        "starts_at": _isopast(6),
        "ends_at":   _isofuture(18),
        "customers_affected": 38_000,
        "reason": (
            "NWS Red Flag forecast — 50+ mph gust threshold expected "
            "in HFTD Tier 3 circuits, Camp Fire ignition corridor."
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-121.78, 39.50], [-121.10, 39.50],
                [-121.10, 40.05], [-121.78, 40.05],
                [-121.78, 39.50],
            ]],
        },
    },
    {
        "zone_id": "pge_sonoma_240507",
        "utility": "PG&E",
        "name": "Sonoma — Geyserville / Healdsburg",
        "status": "scheduled",
        "starts_at": _isofuture(8),
        "ends_at":   _isofuture(28),
        "customers_affected": 22_500,
        "reason": (
            "Forecast offshore wind event. Kincade Fire ignition zone."
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-123.10, 38.55], [-122.70, 38.55],
                [-122.70, 38.95], [-123.10, 38.95],
                [-123.10, 38.55],
            ]],
        },
    },
    {
        "zone_id": "sce_riverside_240507",
        "utility": "SCE",
        "name": "San Bernardino mountains — Big Bear / Idyllwild",
        "status": "active",
        "starts_at": _isopast(3),
        "ends_at":   _isofuture(14),
        "customers_affected": 14_200,
        "reason": (
            "Santa Ana wind event. Elevated dead-fuel moisture + "
            "expected 35-50 mph sustained on south-facing slopes."
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-117.20, 33.95], [-116.70, 33.95],
                [-116.70, 34.30], [-117.20, 34.30],
                [-117.20, 33.95],
            ]],
        },
    },
    {
        "zone_id": "sdge_eastcounty_240507",
        "utility": "SDG&E",
        "name": "East San Diego County — Ramona / Julian",
        "status": "standby",
        "starts_at": _isofuture(20),
        "ends_at":   _isofuture(36),
        "customers_affected": 6_800,
        "reason": (
            "Watch — possible Santa Ana onset. No de-energization yet."
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-116.95, 32.85], [-116.45, 32.85],
                [-116.45, 33.20], [-116.95, 33.20],
                [-116.95, 32.85],
            ]],
        },
    },
    {
        "zone_id": "pge_napa_240507",
        "utility": "PG&E",
        "name": "Napa / Lake — Calistoga corridor",
        "status": "active",
        "starts_at": _isopast(2),
        "ends_at":   _isofuture(20),
        "customers_affected": 11_400,
        "reason": (
            "Glass Fire ignition zone — HFTD Tier 3 conductors at risk."
        ),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-122.75, 38.50], [-122.40, 38.50],
                [-122.40, 38.85], [-122.75, 38.85],
                [-122.75, 38.50],
            ]],
        },
    },
]


def list_zones() -> list[dict]:
    """Return the catalog as-is. Endpoint wraps in a FeatureCollection."""
    return list(KNOWN_PSPS_ZONES)


# Mutual-aid mapping — which utilities a given IOU notifies on
# de-energization. Reflects WECC-region operating agreements: a PG&E
# PSPS in the North Bay notifies SMUD and TID; an SCE event notifies
# LADWP and IID; an SDG&E event notifies CFE south of the border.
MUTUAL_AID_NETWORK = {
    "PG&E":  ["SMUD", "TID", "Northern California IOUs"],
    "SCE":   ["LADWP", "IID", "Anaheim Public Utilities"],
    "SDG&E": ["IID", "CFE (Mexico)"],
    "BVES":  ["SCE"],
}


def neighbors_of(utility: str) -> list[str]:
    """Return the mutual-aid notification list for a given utility."""
    return list(MUTUAL_AID_NETWORK.get(utility, []))
