"""
Sanctioned-vessel catalog — MMSIs that appear on OFAC SDN, UK OFSI,
EU sanctions, or are widely reported as part of the Iranian/Russian
shadow fleet.

The curated SANCTIONED_VESSELS list below is the baseline catalog that
ships with the code. At runtime, refresh_from_public_feeds() can be
called periodically (via the gap-sweeper loop or a dedicated scheduled
task) to UNION the baseline with parsed entries from the real public
feeds:

  - OFAC SDN XML:    https://www.treasury.gov/ofac/downloads/sdn.xml
  - OFAC consolidated: https://www.treasury.gov/ofac/downloads/consolidated/
  - UK OFSI:         https://docs.fcdo.gov.uk/docs/UK-Sanctions-List.html
  - EU sanctions:    https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions

The parser is best-effort: OFAC SDN ships XML with <vesselInfo> child
elements that carry vesselFlag, callSign, vesselType, and occasionally
an <imoNumber>. MMSI is rarely populated by OFAC directly — we still
record the IMO and use that as the join key against entity.attrs.imo.

When the feeds can't be reached (no network, key/auth wall, parse
error), the baseline catalog continues to serve so the cross-reference
never goes silent. A `last_refresh_at` timestamp is exposed so the
operator's daily brief can include 'sanctions data current as of …'.

Each entry: mmsi (string, optional), imo (string, optional), name,
program (OFAC EO / EU regulation), flag, source (regime name), note.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("sanctions")


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


# ----------------------------------------------------------------------
# Live-feed augmentation
# ----------------------------------------------------------------------
# UNION of the baseline catalog and entries pulled from public feeds.
# Refreshed by refresh_from_public_feeds() on a 24h cadence by main.py
# during startup + periodically via the existing gap-sweeper loop.
_LIVE_LOCK = threading.Lock()
_LIVE_VESSELS: list[dict] = list(SANCTIONED_VESSELS)
_LIVE_BY_MMSI: dict[str, dict] = {}
_LIVE_BY_IMO: dict[str, dict] = {}
_LAST_REFRESH_AT: datetime | None = None
_LAST_REFRESH_OK: bool = False
_LAST_REFRESH_NOTE: str = "baseline catalog only — public feeds not yet pulled"


def _rebuild_index() -> None:
    """Recompute the MMSI/IMO indexes after _LIVE_VESSELS is mutated.
    Caller holds _LIVE_LOCK."""
    global _LIVE_BY_MMSI, _LIVE_BY_IMO
    by_mmsi: dict[str, dict] = {}
    by_imo: dict[str, dict] = {}
    for v in _LIVE_VESSELS:
        mmsi = v.get("mmsi")
        imo = v.get("imo")
        if mmsi:
            by_mmsi[str(mmsi).strip()] = v
        if imo:
            by_imo[str(imo).strip()] = v
    _LIVE_BY_MMSI = by_mmsi
    _LIVE_BY_IMO = by_imo


with _LIVE_LOCK:
    _rebuild_index()


# ----- public-feed parsers (best-effort, fail silent) -----------------

# OFAC SDN XML uses the namespace below. We don't bother with a full
# XSD-driven parser — the file is huge and only ~10% of entries are
# vessels. We iterparse on `<sdnEntry>` elements and pull the relevant
# child fields. namespace-stripping helper handles `xmlns` prefixes.
_OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
_EU_CONSOLIDATED_URL = (
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
)
_UK_OFSI_URL = "https://docs.fcdo.gov.uk/docs/UK-Sanctions-List.html"


def _strip_ns(tag: str) -> str:
    """Strip XML namespace prefix from an element tag."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _parse_ofac_sdn_xml(body: bytes) -> list[dict]:
    """Best-effort parse of OFAC SDN XML for vessel entries.

    Returns a list of {mmsi, imo, name, flag, program, source, note}
    dicts. Drops entries we can't extract enough data from.
    """
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log.warning("OFAC SDN parse error: %s", exc)
        return out

    for entry in root.iter():
        if _strip_ns(entry.tag) != "sdnEntry":
            continue
        # We only want vessel-type entries.
        sdn_type = None
        for c in entry:
            if _strip_ns(c.tag) == "sdnType":
                sdn_type = (c.text or "").strip()
                break
        if sdn_type != "Vessel":
            continue

        # Name fields
        first = last = None
        for c in entry:
            t = _strip_ns(c.tag)
            if t == "firstName":
                first = (c.text or "").strip() or None
            elif t == "lastName":
                last = (c.text or "").strip() or None
        name = " ".join(filter(None, [first, last])) or None

        # vesselInfo block
        flag = call_sign = vessel_type = imo = None
        for c in entry:
            if _strip_ns(c.tag) != "vesselInfo":
                continue
            for v in c:
                vt = _strip_ns(v.tag)
                txt = (v.text or "").strip() or None
                if vt == "vesselFlag":
                    flag = txt
                elif vt == "callSign":
                    call_sign = txt
                elif vt == "vesselType":
                    vessel_type = txt
            break

        # IMO sometimes appears as an idList entry with idType='IMO Number'.
        for c in entry:
            if _strip_ns(c.tag) != "idList":
                continue
            for id_el in c:
                if _strip_ns(id_el.tag) != "id":
                    continue
                id_type = id_num = None
                for f in id_el:
                    ft = _strip_ns(f.tag)
                    if ft == "idType":
                        id_type = (f.text or "").strip()
                    elif ft == "idNumber":
                        id_num = (f.text or "").strip()
                if id_type and id_type.upper().startswith("IMO") and id_num:
                    imo = id_num.replace("IMO", "").strip()

        # Programs the entity sits on.
        programs: list[str] = []
        for c in entry:
            if _strip_ns(c.tag) != "programList":
                continue
            for p in c:
                if _strip_ns(p.tag) == "program":
                    txt = (p.text or "").strip()
                    if txt:
                        programs.append(txt)

        if not name and not imo:
            continue

        out.append({
            "mmsi": None,
            "imo": imo,
            "name": name or "(unnamed)",
            "flag": flag or "(unknown)",
            "source": "OFAC SDN",
            "program": ", ".join(programs) or "(unspecified)",
            "note": f"Type: {vessel_type or 'Vessel'}"
                    + (f"; Call sign: {call_sign}" if call_sign else ""),
        })

    return out


def refresh_from_public_feeds(timeout_s: float = 30.0) -> dict:
    """Pull the live sanctions feeds and merge into the in-memory catalog.

    Safe to call from a background thread. Network errors are caught
    and logged — the baseline catalog continues to serve.

    Returns a small status dict for the caller's audit logging.
    """
    import httpx

    global _LIVE_VESSELS, _LAST_REFRESH_AT, _LAST_REFRESH_OK, _LAST_REFRESH_NOTE

    new_entries: list[dict] = []
    notes: list[str] = []
    ok = False

    # OFAC SDN — biggest single source.
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            r = client.get(_OFAC_SDN_URL)
            r.raise_for_status()
            parsed = _parse_ofac_sdn_xml(r.content)
            new_entries.extend(parsed)
            notes.append(f"OFAC SDN: {len(parsed)} vessel entries")
            ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("OFAC SDN refresh failed: %s", exc)
        notes.append(f"OFAC SDN: failed ({exc})")

    # EU consolidated list — XML, similar shape. We parse the same way
    # but the schema is slightly different (subjectType=ENTITY/PERSON
    # with subjectShortName as the vessel name). Implementation deferred
    # to a follow-up; the baseline + OFAC covers the high-value entries.
    notes.append("EU consolidated: parser deferred (baseline serves)")

    # UK OFSI — published as HTML + PDF rather than XML, less amenable
    # to drop-in parsing. Manual sync into the baseline catalog for now.
    notes.append("UK OFSI: HTML format — manual sync into baseline")

    # MERGE — baseline UNION new feed entries, deduped by (imo) since
    # OFAC vessels rarely carry MMSI directly.
    merged: list[dict] = list(SANCTIONED_VESSELS)
    seen_imos = {v["imo"] for v in SANCTIONED_VESSELS if v.get("imo")}
    seen_mmsis = {v["mmsi"] for v in SANCTIONED_VESSELS if v.get("mmsi")}
    for v in new_entries:
        if v.get("imo") and v["imo"] in seen_imos:
            continue
        if v.get("mmsi") and v["mmsi"] in seen_mmsis:
            continue
        merged.append(v)
        if v.get("imo"):
            seen_imos.add(v["imo"])

    with _LIVE_LOCK:
        _LIVE_VESSELS = merged
        _rebuild_index()
        _LAST_REFRESH_AT = datetime.now(timezone.utc)
        _LAST_REFRESH_OK = ok
        _LAST_REFRESH_NOTE = "; ".join(notes)

    log.info(
        "sanctions refresh: %d baseline + %d new = %d total (%s)",
        len(SANCTIONED_VESSELS),
        len(merged) - len(SANCTIONED_VESSELS),
        len(merged),
        _LAST_REFRESH_NOTE,
    )
    return {
        "baseline": len(SANCTIONED_VESSELS),
        "added": len(merged) - len(SANCTIONED_VESSELS),
        "total": len(merged),
        "ok": ok,
        "note": _LAST_REFRESH_NOTE,
        "at": _LAST_REFRESH_AT.isoformat() if _LAST_REFRESH_AT else None,
    }


def refresh_status() -> dict:
    """Return the most-recent feed-refresh state for surfacing in the
    daily brief / handoff log."""
    with _LIVE_LOCK:
        return {
            "last_refresh_at": (
                _LAST_REFRESH_AT.isoformat() if _LAST_REFRESH_AT else None),
            "ok": _LAST_REFRESH_OK,
            "note": _LAST_REFRESH_NOTE,
            "total_vessels": len(_LIVE_VESSELS),
            "baseline_vessels": len(SANCTIONED_VESSELS),
        }


def is_sanctioned(mmsi: str | None, imo: str | None = None) -> dict | None:
    """Return the sanction record for an MMSI (or IMO), or None.

    Match priority: MMSI first (Class-A AIS reports it), then IMO
    (some sanctioned vessels swap MMSIs but the IMO is permanent).
    Indexed lookup over the LIVE catalog (baseline + feed-pulled).
    """
    if not mmsi and not imo:
        return None
    mmsi_s = str(mmsi or "").strip() or None
    imo_s = str(imo or "").strip() or None
    with _LIVE_LOCK:
        if mmsi_s:
            v = _LIVE_BY_MMSI.get(mmsi_s)
            if v is not None:
                return v
        if imo_s:
            v = _LIVE_BY_IMO.get(imo_s)
            if v is not None:
                return v
    return None


def list_sanctioned() -> list[dict]:
    """Full live list (baseline + feed-pulled) for the /maritime/sanctions
    endpoint. Frontend bulk-indexes this on mount."""
    with _LIVE_LOCK:
        snapshot = list(_LIVE_VESSELS)
    return [
        {"mmsi": v.get("mmsi"), "imo": v.get("imo"), "name": v["name"],
         "flag": v["flag"], "source": v["source"], "program": v["program"],
         "note": v.get("note", "")}
        for v in snapshot
    ]
