"""
Onshore asset catalog — Coast Guard stations and Navy facilities along
the Gulf of Mexico. Used by the frontend overlay so an operator can
immediately see what surface assets are nearby a dispatch target.

Each entry: name, type (uscg / navy), lon, lat, and a short note.
Coordinates are at the gate/main pier — close enough for "X km away"
distance estimates, not for routing.

Source: NOAA chartlets + USCG/Navy public facility lists. The set is
intentionally curated to the major operational stations relevant to a
Texas/Louisiana shoreline demo; expanding to every small-boat station
would clutter the map without adding signal.
"""

from __future__ import annotations


ONSHORE_ASSETS: list[dict] = [
    # ---- USCG District 8 (Gulf of Mexico) major stations ----
    {
        "name": "USCG Sector Houston-Galveston",
        "type": "uscg",
        "lon": -94.7826, "lat": 29.3036,
        "note": "Largest USCG sector on the Gulf. Two cutters home-ported.",
    },
    {
        "name": "USCG Station Galveston",
        "type": "uscg",
        "lon": -94.7937, "lat": 29.3076,
        "note": "Small-boat station, primary SAR for Galveston Bay approach.",
    },
    {
        "name": "USCG Station Sabine",
        "type": "uscg",
        "lon": -93.8800, "lat": 29.7297,
        "note": "Boat station covering the Sabine river entrance.",
    },
    {
        "name": "USCG Station Port Arthur",
        "type": "uscg",
        "lon": -93.9320, "lat": 29.8635,
        "note": "Co-located with the port; aids-to-navigation tender base.",
    },
    {
        "name": "USCG Sector Corpus Christi",
        "type": "uscg",
        "lon": -97.3964, "lat": 27.8133,
        "note": "Sector HQ for South Texas. Cutter Manowar home-ported.",
    },
    {
        "name": "USCG Station South Padre Island",
        "type": "uscg",
        "lon": -97.1670, "lat": 26.0734,
        "note": "Southernmost continental US station, RGV migrant ops focus.",
    },
    {
        "name": "USCG Air Station Houston",
        "type": "uscg",
        "lon": -95.2789, "lat": 29.6080,
        "note": "MH-65 Dolphin helicopters — fast SAR + interdiction response.",
    },
    {
        "name": "USCG Air Station Corpus Christi",
        "type": "uscg",
        "lon": -97.2812, "lat": 27.7706,
        "note": "MH-65 Dolphin + HC-144 Ocean Sentry fixed-wing patrol.",
    },
    {
        "name": "USCG Sector New Orleans",
        "type": "uscg",
        "lon": -90.0589, "lat": 29.9619,
        "note": "Lower Miss & Louisiana coast oversight; multiple cutters.",
    },
    {
        "name": "USCG Air Station New Orleans",
        "type": "uscg",
        "lon": -90.0250, "lat": 29.8253,
        "note": "MH-60 Jayhawk + HC-144; longer-range SAR than MH-65.",
    },
    {
        "name": "USCG Sector Mobile",
        "type": "uscg",
        "lon": -88.0399, "lat": 30.6954,
        "note": "AL/MS/FL panhandle. Cutter Decisive home-ported.",
    },
    {
        "name": "USCG Air Station Mobile",
        "type": "uscg",
        "lon": -88.2400, "lat": 30.6244,
        "note": "MH-65 Dolphin + HC-144 Ocean Sentry.",
    },

    # ---- US Navy & joint facilities ----
    {
        "name": "NAS Corpus Christi",
        "type": "navy",
        "lon": -97.2867, "lat": 27.6927,
        "note": "Primary jet training; T-44C/T-45C; co-located with USCG ASCC.",
    },
    {
        "name": "NAS Kingsville",
        "type": "navy",
        "lon": -97.8089, "lat": 27.5072,
        "note": "Strike training. Inland but Gulf-deployable on short notice.",
    },
    {
        "name": "Naval Air Station Pensacola",
        "type": "navy",
        "lon": -87.3169, "lat": 30.3502,
        "note": "Cradle of Naval Aviation; multiple training sqs + the Blue Angels.",
    },
    {
        "name": "Naval Air Station JRB New Orleans (Belle Chasse)",
        "type": "navy",
        "lon": -90.0211, "lat": 29.8253,
        "note": "Joint Reserve Base — Navy P-8 maritime patrol det. periodically.",
    },
    {
        "name": "Naval Construction Battalion Center Gulfport",
        "type": "navy",
        "lon": -89.0825, "lat": 30.3917,
        "note": "Seabee deployment center; expeditionary engineering.",
    },
]


def list_assets() -> list[dict]:
    """Return the asset catalog for the /maritime/onshore_assets endpoint."""
    return list(ONSHORE_ASSETS)
