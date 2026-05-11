"""
MMSI → Maritime Identification Digit → country/flag-state lookup.

The first three digits of a Class-A or static-data AIS MMSI form the
MID (Maritime Identification Digit), which the ITU assigns per
country. We use it to enrich entity attrs with flag state — operators
need to know whether a suspicious vessel is flying a Liberian flag
of convenience, a Russian flag, etc.

The MID table is taken from ITU-R M.585 (Assignment and use of
identities in the maritime mobile service), 2024 edition. Only a
subset of MIDs are listed — the most-common flag states in Gulf
traffic + sanctioned-fleet origins. Unknown MMSIs return None,
which the frontend renders as "(flag unknown)" without breaking.

When a paid Equasis / MarineTraffic API key is available, the
backend endpoint that uses this falls back to it for owner/IMO
fields; today it just returns MID-derived flag state.
"""

from __future__ import annotations


# (mid_prefix → {country, iso2}). Subset that covers Gulf traffic +
# common shadow-fleet origins. Add entries as needed; the table is
# small enough to scan linearly without indexing.
_MID_TABLE: dict[int, dict] = {
    # North America
    303: {"country": "Alaska (USA)",            "iso2": "US"},
    338: {"country": "United States",           "iso2": "US"},
    366: {"country": "United States",           "iso2": "US"},
    367: {"country": "United States",           "iso2": "US"},
    368: {"country": "United States",           "iso2": "US"},
    369: {"country": "United States",           "iso2": "US"},
    316: {"country": "Canada",                  "iso2": "CA"},
    345: {"country": "Mexico",                  "iso2": "MX"},
    373: {"country": "Mexico",                  "iso2": "MX"},
    # Central America & Caribbean
    339: {"country": "Jamaica",                 "iso2": "JM"},
    317: {"country": "Cayman Islands",          "iso2": "KY"},
    323: {"country": "Cuba",                    "iso2": "CU"},
    341: {"country": "Saint Kitts and Nevis",   "iso2": "KN"},
    372: {"country": "Honduras",                "iso2": "HN"},
    # Flags of convenience (frequent in Gulf traffic)
    351: {"country": "Panama",                  "iso2": "PA"},
    352: {"country": "Panama",                  "iso2": "PA"},
    353: {"country": "Panama",                  "iso2": "PA"},
    354: {"country": "Panama",                  "iso2": "PA"},
    355: {"country": "Panama",                  "iso2": "PA"},
    356: {"country": "Panama",                  "iso2": "PA"},
    357: {"country": "Panama",                  "iso2": "PA"},
    370: {"country": "Panama",                  "iso2": "PA"},
    371: {"country": "Panama",                  "iso2": "PA"},
    374: {"country": "Panama",                  "iso2": "PA"},
    636: {"country": "Liberia",                 "iso2": "LR"},
    637: {"country": "Liberia",                 "iso2": "LR"},
    538: {"country": "Marshall Islands",        "iso2": "MH"},
    311: {"country": "Bahamas",                 "iso2": "BS"},
    312: {"country": "Belize",                  "iso2": "BZ"},
    229: {"country": "Malta",                   "iso2": "MT"},
    215: {"country": "Malta",                   "iso2": "MT"},
    256: {"country": "Malta",                   "iso2": "MT"},
    248: {"country": "Malta",                   "iso2": "MT"},
    249: {"country": "Malta",                   "iso2": "MT"},
    257: {"country": "Norway",                  "iso2": "NO"},
    258: {"country": "Norway",                  "iso2": "NO"},
    259: {"country": "Norway",                  "iso2": "NO"},
    219: {"country": "Denmark",                 "iso2": "DK"},
    220: {"country": "Denmark",                 "iso2": "DK"},
    231: {"country": "Faroe Islands",           "iso2": "FO"},
    # Europe
    232: {"country": "United Kingdom",          "iso2": "GB"},
    233: {"country": "United Kingdom",          "iso2": "GB"},
    234: {"country": "United Kingdom",          "iso2": "GB"},
    235: {"country": "United Kingdom",          "iso2": "GB"},
    211: {"country": "Germany",                 "iso2": "DE"},
    218: {"country": "Germany",                 "iso2": "DE"},
    227: {"country": "France",                  "iso2": "FR"},
    228: {"country": "France",                  "iso2": "FR"},
    226: {"country": "France",                  "iso2": "FR"},
    247: {"country": "Italy",                   "iso2": "IT"},
    224: {"country": "Spain",                   "iso2": "ES"},
    225: {"country": "Spain",                   "iso2": "ES"},
    244: {"country": "Netherlands",             "iso2": "NL"},
    245: {"country": "Netherlands",             "iso2": "NL"},
    246: {"country": "Netherlands",             "iso2": "NL"},
    253: {"country": "Belgium",                 "iso2": "BE"},
    266: {"country": "Sweden",                  "iso2": "SE"},
    265: {"country": "Sweden",                  "iso2": "SE"},
    230: {"country": "Finland",                 "iso2": "FI"},
    240: {"country": "Greece",                  "iso2": "GR"},
    241: {"country": "Greece",                  "iso2": "GR"},
    237: {"country": "Greece",                  "iso2": "GR"},
    239: {"country": "Greece",                  "iso2": "GR"},
    277: {"country": "Lithuania",               "iso2": "LT"},
    276: {"country": "Estonia",                 "iso2": "EE"},
    275: {"country": "Latvia",                  "iso2": "LV"},
    271: {"country": "Türkiye",                 "iso2": "TR"},
    # Russia + commonly-sanctioned shadow-fleet origins
    273: {"country": "Russian Federation",      "iso2": "RU"},
    422: {"country": "Iran",                    "iso2": "IR"},
    423: {"country": "Azerbaijan",              "iso2": "AZ"},
    459: {"country": "Tajikistan",              "iso2": "TJ"},
    421: {"country": "Yemen",                   "iso2": "YE"},
    403: {"country": "Saudi Arabia",            "iso2": "SA"},
    470: {"country": "United Arab Emirates",    "iso2": "AE"},
    471: {"country": "United Arab Emirates",    "iso2": "AE"},
    501: {"country": "Adelie Land (FR)",        "iso2": "FR"},
    # Asia (large registry)
    412: {"country": "China",                   "iso2": "CN"},
    413: {"country": "China",                   "iso2": "CN"},
    414: {"country": "China",                   "iso2": "CN"},
    441: {"country": "Korea (South)",           "iso2": "KR"},
    440: {"country": "Korea (South)",           "iso2": "KR"},
    432: {"country": "Japan",                   "iso2": "JP"},
    431: {"country": "Japan",                   "iso2": "JP"},
    525: {"country": "Indonesia",               "iso2": "ID"},
    563: {"country": "Singapore",               "iso2": "SG"},
    564: {"country": "Singapore",               "iso2": "SG"},
    565: {"country": "Singapore",               "iso2": "SG"},
    566: {"country": "Singapore",               "iso2": "SG"},
    477: {"country": "Hong Kong",               "iso2": "HK"},
    416: {"country": "Taiwan",                  "iso2": "TW"},
    443: {"country": "Palestine (State of)",    "iso2": "PS"},
    427: {"country": "Israel",                  "iso2": "IL"},
    # South America (major Gulf traffic partners)
    701: {"country": "Argentina",               "iso2": "AR"},
    710: {"country": "Brazil",                  "iso2": "BR"},
    725: {"country": "Chile",                   "iso2": "CL"},
    730: {"country": "Colombia",                "iso2": "CO"},
    735: {"country": "Ecuador",                 "iso2": "EC"},
    760: {"country": "Peru",                    "iso2": "PE"},
    775: {"country": "Venezuela",               "iso2": "VE"},
}


def mid_country(mmsi: str | None) -> dict | None:
    """Decode the MID prefix of an MMSI string to a flag state.

    Returns None when the MMSI is missing, malformed, or carries a
    MID not in our lookup table (we cover the common Gulf + shadow-
    fleet flags; outside that you get None).
    """
    if not mmsi:
        return None
    s = str(mmsi).strip()
    if len(s) < 3 or not s[:3].isdigit():
        return None
    return _MID_TABLE.get(int(s[:3]))


# Quick stats on the type of MMSI (Class A vessel, base station, SAR,
# Aids to Navigation, etc). Inferred from the high-order digits per
# ITU-R M.585.
def mmsi_kind(mmsi: str | None) -> str:
    """Return a short label describing what kind of MMSI this is."""
    if not mmsi:
        return "unknown"
    s = str(mmsi).strip()
    if len(s) != 9 or not s.isdigit():
        return "unknown"
    if s.startswith("00"):
        return "Coast station"
    if s.startswith("0"):
        return "Group of ships"
    if s.startswith("111"):
        return "SAR aircraft"
    if s.startswith("970"):
        return "AIS-SART"
    if s.startswith("972"):
        return "Man overboard device"
    if s.startswith("974"):
        return "EPIRB"
    if s.startswith("98"):
        return "Auxiliary craft"
    if s.startswith("99"):
        return "Aid to navigation"
    return "Ship station"
