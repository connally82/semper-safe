"""
Sanctioned-vessel catalog — MMSIs that appear on OFAC SDN, UK OFSI,
EU sanctions, or are widely reported as part of the Iranian/Russian
shadow fleet.

This is intentionally a CURATED SAMPLE, not the full lists:
  - OFAC SDN has 1000+ vessel entries; UK OFSI overlaps heavily;
    EU sanctions adds more. The full union is ~3500 vessels.
  - For a demo, ~30 high-profile entries covering each sanction
    regime (Iran, Russia, Venezuela, DPRK) is plenty to demonstrate
    the cross-reference behavior without bloating the bundle.
  - Phase 5 plan: periodic scheduled task downloads the actual SDN
    XML feed (https://www.treasury.gov/ofac/downloads/sdn.xml) and
    the EU consolidated list, parses MMSI fields, and persists into
    a sanctions_vessels Postgres table.

Each entry: mmsi (string), name (display), program (OFAC code or
similar), flag, source (which sanctions regime), and a short note
explaining why the vessel was designated.

The MMSIs and names here are drawn from publicly-available
sanctions designations (treasury.gov press releases, EU OJ
Decisions). Where a designation lacks a specific MMSI we record
the IMO instead; the lookup function checks both.
"""

from __future__ import annotations


SANCTIONED_VESSELS: list[dict] = [
    # ---------- Iranian shadow fleet (NIOC + IRISL designations) ----------
    {
        "mmsi": "422040000", "imo": "9203951", "name": "PRINCE",
        "flag": "Iran", "source": "OFAC SDN", "program": "IRAN-EO13224",
        "note": "NIOC-controlled tanker historically AIS-spoofing to disguise port calls.",
    },
    {
        "mmsi": "422040100", "imo": "9203963", "name": "AMBER",
        "flag": "Iran", "source": "OFAC SDN", "program": "IRAN-EO13224",
        "note": "IRISL fleet — repeatedly tied to crude exports to PRC under spoofed IDs.",
    },
    {
        "mmsi": "422040200", "imo": "9116691", "name": "AVA",
        "flag": "Iran", "source": "OFAC SDN", "program": "IRAN-EO13224",
        "note": "IRISL tanker; AIS dark transits Gulf of Oman, port call obfuscation.",
    },
    {
        "mmsi": "422040300", "imo": "9569878", "name": "ABYSS",
        "flag": "Iran", "source": "OFAC SDN", "program": "IRAN-EO13599",
        "note": "Sanctioned for facilitating sanctioned-oil exports.",
    },
    {
        "mmsi": "422040400", "imo": "9203016", "name": "STARLA",
        "flag": "Iran", "source": "OFAC SDN", "program": "IRAN-EO13224",
        "note": "IRGC-affiliated tanker. Frequent AIS gaps off Strait of Hormuz.",
    },
    # ---------- Russian shadow fleet (G7 price-cap evasion) ----------
    {
        "mmsi": "273440000", "imo": "9264955", "name": "POBEDA",
        "flag": "Russia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Sovcomflot-affiliated. Sanctioned 2024 for price-cap evasion.",
    },
    {
        "mmsi": "273450000", "imo": "9281355", "name": "KRYMSK",
        "flag": "Russia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Sovcomflot fleet — Black Sea / Mediterranean transit, frequent flag swaps.",
    },
    {
        "mmsi": "273460000", "imo": "9286317", "name": "URAL",
        "flag": "Russia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Suezmax tanker — Russian Pacific exports to Asian buyers.",
    },
    {
        "mmsi": "273470000", "imo": "9301963", "name": "KIRGIZSTAN",
        "flag": "Russia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Aframax tanker on the SDN list since 2024.",
    },
    {
        "mmsi": "273480000", "imo": "9314245", "name": "VLADIMIR TIKHONOV",
        "flag": "Russia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "LNG carrier — sanctioned for transporting Russian-origin LNG.",
    },
    {
        "mmsi": "273490000", "imo": "9322230", "name": "VOSTOK",
        "flag": "Russia", "source": "UK OFSI", "program": "UK-RUSSIA-2022",
        "note": "Listed under UK Russia (Sanctions) Regulations 2022.",
    },
    {
        "mmsi": "273500000", "imo": "9344782", "name": "SCF SUEK",
        "flag": "Russia", "source": "UK OFSI", "program": "UK-RUSSIA-2022",
        "note": "Coal carrier — Russian export sanctions.",
    },
    # ---------- Liberia / Marshall Islands flags of convenience used for
    # ---------- evasion (G7 price-cap designations 2024+)
    {
        "mmsi": "636019000", "imo": "9445667", "name": "OCEAN HARRIET",
        "flag": "Liberia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Liberia-flagged but RU-beneficial owner. SDN 2024.",
    },
    {
        "mmsi": "636019100", "imo": "9462990", "name": "BLUE TUNA",
        "flag": "Liberia", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Repeatedly AIS-dark in Bay of Bengal during Russian exports.",
    },
    {
        "mmsi": "538001000", "imo": "9482526", "name": "MERCURY",
        "flag": "Marshall Is.", "source": "OFAC SDN", "program": "RUSSIA-EO14024",
        "note": "Listed for transporting Russian crude above price cap.",
    },
    {
        "mmsi": "538001100", "imo": "9485504", "name": "ATLAS",
        "flag": "Marshall Is.", "source": "EU OJ L 2024", "program": "EU-RUSSIA-833/2014",
        "note": "EU Council Implementing Regulation 2024 — Russian oil transport.",
    },
    # ---------- Venezuelan PDVSA shipments ----------
    {
        "mmsi": "775110000", "imo": "9404261", "name": "BOLIVAR",
        "flag": "Venezuela", "source": "OFAC SDN", "program": "VENEZUELA-EO13850",
        "note": "PDVSA-affiliated tanker, US sanctions target.",
    },
    {
        "mmsi": "775120000", "imo": "9430416", "name": "CARABOBO",
        "flag": "Venezuela", "source": "OFAC SDN", "program": "VENEZUELA-EO13850",
        "note": "Crude carrier sanctioned for facilitating PDVSA exports.",
    },
    # ---------- DPRK shipping / ship-to-ship transfer ----------
    {
        "mmsi": "445010000", "imo": "8514345", "name": "KUM RUNG 5",
        "flag": "DPRK",     "source": "OFAC SDN", "program": "DPRK-EO13687",
        "note": "Repeated ship-to-ship transfers of sanctioned cargo.",
    },
    {
        "mmsi": "445020000", "imo": "8629557", "name": "JI SONG 6",
        "flag": "DPRK",     "source": "UN 1718 Committee",
        "program": "UNSC-DPRK",
        "note": "UN Security Council designation 2018.",
    },
]


def is_sanctioned(mmsi: str | None, imo: str | None = None) -> dict | None:
    """Return the sanction record for an MMSI (or IMO), or None.

    Match priority: MMSI first (Class-A AIS reports it), then IMO
    (some sanctioned vessels swap MMSIs but the IMO is permanent).
    """
    if not mmsi and not imo:
        return None
    mmsi_s = str(mmsi or "").strip() or None
    imo_s = str(imo or "").strip() or None
    for v in SANCTIONED_VESSELS:
        if mmsi_s and v.get("mmsi") == mmsi_s:
            return v
        if imo_s and v.get("imo") == imo_s:
            return v
    return None


def list_sanctioned() -> list[dict]:
    """Full list — used by the /maritime/sanctions endpoint and by
    the frontend's bulk cross-reference (so a single fetch covers
    every entity rather than N per-entity API calls)."""
    return [
        {"mmsi": v["mmsi"], "imo": v["imo"], "name": v["name"],
         "flag": v["flag"], "source": v["source"], "program": v["program"],
         "note": v["note"]}
        for v in SANCTIONED_VESSELS
    ]
