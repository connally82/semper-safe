"""
Gulf of Mexico major-port catalog.

Used by fusion.detect_port_skipping() — when a vessel's AIS-declared
destination matches one of these ports and its heading doesn't lead
there, we flag the vessel as port_skipping. Real maritime ops signal
for sanctioned-fleet vessels (declare a legitimate destination, run
elsewhere) and smugglers (clear a port for paperwork, divert at sea).

Coordinates are the canonical port-mouth waypoint — vessels usually
slow to pilot speed near the mouth before entering the channel, so
the bearing-to-port comparison stays sane even as the vessel
approaches close inshore.

Aliases cover the common ways vessels report destinations on AIS:
formal port name ("HOUSTON"), short code ("HOU"), country-tagged
variant ("HOUSTON TX"), and a few well-known terminals within the
port complex (CB1, EBT, BARBOURS CUT).
"""

from __future__ import annotations


# (canonical name, lon, lat, [aliases...])
KNOWN_GULF_PORTS: list[dict] = [
    {
        "name": "Houston",
        "lon": -95.0140, "lat": 29.7244,
        "aliases": [
            "HOUSTON", "HOUSTON TX", "HOU", "HOUSTON,TX", "HOUSTON,TX,US",
            "USHOU", "PORT OF HOUSTON", "BARBOURS CUT", "BAYPORT",
            "BAYPORT TERMINAL", "BAYPORT CONTAINER", "BARBOURS",
            "CB1", "CB2", "CB3",
        ],
    },
    {
        "name": "Galveston",
        "lon": -94.7847, "lat": 29.3013,
        "aliases": ["GALVESTON", "GAL", "GALVESTON TX", "USGAL"],
    },
    {
        "name": "Texas City",
        "lon": -94.9007, "lat": 29.3819,
        "aliases": ["TEXAS CITY", "TEX CITY", "TEX-CITY", "USTXC"],
    },
    {
        "name": "Freeport",
        "lon": -95.3045, "lat": 28.9425,
        "aliases": ["FREEPORT", "FREEPORT TX", "USFRP"],
    },
    {
        "name": "Corpus Christi",
        "lon": -97.3964, "lat": 27.8133,
        "aliases": [
            "CORPUS", "CORPUS CHRISTI", "CC", "CC TX", "USCRP",
            "PORT ARANSAS", "ARANSAS PASS",
        ],
    },
    {
        "name": "Brownsville",
        "lon": -97.4031, "lat": 25.9518,
        "aliases": ["BROWNSVILLE", "BRO", "BROWNSVILLE TX", "USBRO"],
    },
    {
        "name": "Port Arthur",
        "lon": -93.9320, "lat": 29.8635,
        "aliases": [
            "PORT ARTHUR", "PORTARTHUR", "USPOA",
            "BEAUMONT", "SABINE", "SABINE PASS",
        ],
    },
    {
        "name": "Lake Charles",
        "lon": -93.2174, "lat": 30.2266,
        "aliases": ["LAKE CHARLES", "LC", "USLAK"],
    },
    {
        "name": "New Orleans",
        "lon": -90.0658, "lat": 29.9511,
        "aliases": [
            "NEW ORLEANS", "NEWORLEANS", "NOLA", "USNEW",
            "SOUTHWEST PASS", "SW PASS",
        ],
    },
    {
        "name": "Mobile",
        "lon": -88.0399, "lat": 30.6954,
        "aliases": ["MOBILE", "MOBILE AL", "MOB", "USMOB"],
    },
    {
        "name": "Pensacola",
        "lon": -87.2169, "lat": 30.3935,
        "aliases": ["PENSACOLA", "PNS", "USPNS"],
    },
    {
        "name": "Tampa",
        "lon": -82.4572, "lat": 27.9506,
        "aliases": ["TAMPA", "TAMPA FL", "USTPA"],
    },
    {
        "name": "Veracruz",
        "lon": -96.1230, "lat": 19.1738,
        "aliases": ["VERACRUZ", "VER", "MXVER"],
    },
    {
        "name": "Tampico",
        "lon": -97.7857, "lat": 22.2294,
        "aliases": ["TAMPICO", "TAM", "MXTAM"],
    },
    {
        "name": "Tuxpan",
        "lon": -97.3236, "lat": 20.9670,
        "aliases": ["TUXPAN", "TUX", "MXTUX"],
    },
    {
        "name": "Coatzacoalcos",
        "lon": -94.4124, "lat": 18.1444,
        "aliases": ["COATZACOALCOS", "COA", "MXCOA"],
    },
    {
        "name": "Progreso",
        "lon": -89.6650, "lat": 21.3300,
        "aliases": ["PROGRESO", "PRO", "MXPRO"],
    },
]


def match_port_by_destination(dest: str | None) -> dict | None:
    """Best-effort match an AIS-reported `destination` field to a known
    Gulf port.

    AIS destination strings are operator-entered free-form text — the
    field is 20 chars max, often abbreviated, sometimes mis-typed. We
    do a case-insensitive substring check against the canonical name
    AND every alias, and return the first port that matches.

    Returns the matching port dict or None.
    """
    if not dest:
        return None
    d = dest.upper().strip()
    if not d:
        return None
    for port in KNOWN_GULF_PORTS:
        if port["name"].upper() in d or d in port["name"].upper():
            return port
        for alias in port["aliases"]:
            if alias in d or d in alias:
                return port
    return None


def list_ports() -> list[dict]:
    """Returns the full port list. Used by /maritime/ports endpoint
    if/when we expose it for frontend overlay rendering."""
    return [
        {"name": p["name"], "lon": p["lon"], "lat": p["lat"]}
        for p in KNOWN_GULF_PORTS
    ]
