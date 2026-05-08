"""
NOAA Marine Cadastre AIS archive ingestor (sensor enh #2).

Why this exists:
  AISStream gives us live AIS with a 24-hour retention. SAR scenes
  acquired more than 24 hours before processing have no AIS in our DB
  to fuse against, so the SAR-AIS fusion step produces all-dark
  detections regardless of whether real cooperative traffic was there
  during the SAR pass. Marine Cadastre publishes a free, public daily
  CSV archive of all US-territorial-water AIS dating back to 2009 —
  perfect for backfilling fusion on past scenes.

Source:
  https://coast.noaa.gov/htdata/CMSP/AISDataHandler/{year}/AIS_{year}_{mm}_{dd}.zip
  ~300-500 MB compressed per day, ~3-5 GB uncompressed CSV. Records
  contain MMSI, timestamp, lat, lon, SOG, COG, heading, vessel name,
  IMO, callsign, vessel type, status, length, width, draft, cargo.

Pipeline:
  1. Build the daily URL and stream-download to /tmp (Fly tmpfs has
     plenty of room; we delete on exit).
  2. zipfile.ZipFile + csv.DictReader stream-read the entry without
     materializing the full 3-5 GB CSV.
  3. Filter row-by-row: AOI bbox + time window. Yields normalized dicts.

What this version does NOT do (Phase 5):
  - Multi-day windows (today: one daily file = one UTC day).
  - Vessel-static dedupe (today: emit one obs per CSV row, even if
    name/static didn't change between rows).
  - Cross-source identity reconciliation (NOAA + AISStream may report
    the same MMSI). Fusion engine handles this via _mmsi_index already.

Coverage caveat:
  The Marine Cadastre archive lags real-time. As of this writing the
  latest available daily file is 2024-12-31. Daily files for newer
  dates return HTTP 404 until NOAA's pipeline catches up (typically
  a few months). For 2026 scenes the backfill helper surfaces the
  404 with a "try again later" message.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

log = logging.getLogger("noaa_ais")

NOAA_BASE = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"

# Texas-shoreline AOI per memory/semper_safe_aoi.md
TX_AOI_LON_MIN, TX_AOI_LON_MAX = -98.0, -93.5
TX_AOI_LAT_MIN, TX_AOI_LAT_MAX = 25.5, 30.5


@dataclass
class AisRow:
    mmsi: str
    t: datetime
    lat: float
    lon: float
    sog_kn: float | None
    cog_deg: float | None
    heading_deg: float | None
    name: str | None
    imo: str | None
    callsign: str | None
    vessel_type: int | None
    nav_status: int | None
    length_m: float | None
    width_m: float | None
    draft_m: float | None
    cargo: int | None


def daily_zip_url(date: datetime) -> str:
    return (f"{NOAA_BASE}/{date.year}/"
            f"AIS_{date.year}_{date.month:02d}_{date.day:02d}.zip")


def daily_archive_exists(date: datetime, *, timeout_s: float = 20.0) -> bool:
    """HEAD-probe NOAA's daily file. Returns True iff HTTP 200."""
    url = daily_zip_url(date)
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "Semper-Safe/0.1 (luke@entreforlife.com)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def _stream_download_zip(url: str, dest_path: str, *,
                         timeout_s: float = 600.0) -> int:
    """Download a (potentially-multi-hundred-MB) zip to a local path,
    streaming through urllib's response so memory stays bounded.

    Returns total bytes written.
    """
    log.info("downloading NOAA AIS archive: %s", url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Semper-Safe/0.1 (luke@entreforlife.com)"},
    )
    n = 0
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        with open(dest_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)   # 1 MB chunks
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
    log.info("downloaded %d MB to %s", n // (1 << 20), dest_path)
    return n


def _parse_float(v: str) -> float | None:
    if v in ("", "null", "NULL"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_int(v: str) -> int | None:
    if v in ("", "null", "NULL"):
        return None
    try:
        return int(float(v))   # tolerates "5.0" style
    except (TypeError, ValueError):
        return None


def _parse_dt(v: str) -> datetime | None:
    """NOAA timestamps look like '2024-06-15T00:00:01' (UTC, no tz)."""
    if not v:
        return None
    try:
        # Some rows may include trailing 'Z' or fractional secs.
        if v.endswith("Z"):
            v = v[:-1]
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def iter_filtered_rows(zip_path: str, *,
                        time_lo: datetime, time_hi: datetime,
                        lon_min: float = TX_AOI_LON_MIN,
                        lon_max: float = TX_AOI_LON_MAX,
                        lat_min: float = TX_AOI_LAT_MIN,
                        lat_max: float = TX_AOI_LAT_MAX,
                        ) -> Iterator[AisRow]:
    """Stream-read the CSV inside the zip, yield AisRows that fall in
    the time window AND AOI bbox.

    The CSV file inside is a single AIS_*.csv. csv.DictReader handles
    quoting + the column header. We reject rows where the timestamp
    or geom is unparseable, but never load the whole file — peak
    memory is one row at a time.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        csv_name = next((n for n in names if n.lower().endswith(".csv")), None)
        if csv_name is None:
            raise RuntimeError(f"no CSV inside {zip_path}")
        with zf.open(csv_name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8",
                                                     errors="replace"))
            for row in reader:
                t = _parse_dt(row.get("BaseDateTime", ""))
                if t is None or not (time_lo <= t <= time_hi):
                    continue
                lat = _parse_float(row.get("LAT", ""))
                lon = _parse_float(row.get("LON", ""))
                if lat is None or lon is None:
                    continue
                if not (lon_min <= lon <= lon_max
                        and lat_min <= lat <= lat_max):
                    continue
                yield AisRow(
                    mmsi=row.get("MMSI", "").strip(),
                    t=t,
                    lat=lat, lon=lon,
                    sog_kn=_parse_float(row.get("SOG", "")),
                    cog_deg=_parse_float(row.get("COG", "")),
                    heading_deg=_parse_float(row.get("Heading", "")),
                    name=(row.get("VesselName") or "").strip() or None,
                    imo=(row.get("IMO") or "").strip() or None,
                    callsign=(row.get("CallSign") or "").strip() or None,
                    vessel_type=_parse_int(row.get("VesselType", "")),
                    nav_status=_parse_int(row.get("Status", "")),
                    length_m=_parse_float(row.get("Length", "")),
                    width_m=_parse_float(row.get("Width", "")),
                    draft_m=_parse_float(row.get("Draft", "")),
                    cargo=_parse_int(row.get("Cargo", "")),
                )


def fetch_window(time_lo: datetime, time_hi: datetime, *,
                 bbox: tuple[float, float, float, float] | None = None,
                 ) -> dict[str, Any]:
    """Pull NOAA daily file(s) covering [time_lo, time_hi] and yield
    filtered AisRows for the AOI bbox.

    Returns a dict with stats — counts per UTC date file, total kept,
    plus a 'rows' iterator the caller can consume. Caller is
    responsible for consuming or discarding.

    For efficiency this materializes all rows into memory (typically
    a few thousand for a ±30-min Texas window). Caller can switch to
    a streaming generator if a wider window is needed.
    """
    lon_min, lon_max, lat_min, lat_max = (
        bbox if bbox is not None
        else (TX_AOI_LON_MIN, TX_AOI_LON_MAX,
              TX_AOI_LAT_MIN, TX_AOI_LAT_MAX)
    )
    # Walk each UTC day the window touches.
    days_needed = []
    d = time_lo.astimezone(timezone.utc).date()
    end_d = time_hi.astimezone(timezone.utc).date()
    while d <= end_d:
        days_needed.append(d)
        d = d + timedelta(days=1)

    rows: list[AisRow] = []
    per_day_counts: dict[str, int] = {}
    missing_days: list[str] = []

    for day in days_needed:
        date_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        if not daily_archive_exists(date_dt):
            missing_days.append(day.isoformat())
            continue
        # Save to a unique tmp path; clean up after.
        with tempfile.NamedTemporaryFile(
            prefix=f"noaa_ais_{day.isoformat()}_", suffix=".zip",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            _stream_download_zip(daily_zip_url(date_dt), tmp_path)
            n_kept = 0
            for r in iter_filtered_rows(
                tmp_path,
                time_lo=time_lo, time_hi=time_hi,
                lon_min=lon_min, lon_max=lon_max,
                lat_min=lat_min, lat_max=lat_max,
            ):
                rows.append(r)
                n_kept += 1
            per_day_counts[day.isoformat()] = n_kept
            log.info("day %s: kept %d rows in window+AOI", day.isoformat(), n_kept)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return {
        "rows": rows,
        "per_day_counts": per_day_counts,
        "missing_days": missing_days,
        "total_kept": len(rows),
        "n_unique_mmsi": len({r.mmsi for r in rows if r.mmsi}),
    }


# --- engine ingest ---------------------------------------------------------

def ingest_into_engine(rows: list[AisRow], *, engine, batch_size: int = 500
                       ) -> dict[str, int]:
    """Feed each AisRow to engine.ingest as Observation(source=AIS).

    Existing engine code (fusion._ingest_ais) handles MMSI dedup,
    entity creation, and audit logging. After this call the engine has
    historical AIS positions for the time window — the SAR-AIS fusion
    pass will then find matches.

    Returns counts dict.
    """
    import h3
    import uuid as _uuid
    from models import Geom, Observation, SourceType

    n_ingested = 0
    n_skipped = 0
    H3_RES = 8
    for r in rows:
        if not r.mmsi:
            n_skipped += 1
            continue
        try:
            obs = Observation(
                obs_id=f"obs_noaa_{_uuid.uuid4().hex[:12]}",
                source=SourceType.AIS,
                source_id=r.mmsi,
                geom=Geom(lon=r.lon, lat=r.lat),
                h3_cell=h3.latlng_to_cell(r.lat, r.lon, H3_RES),
                t=r.t,
                attrs={
                    "name": r.name, "imo": r.imo, "callsign": r.callsign,
                    "vessel_type": r.vessel_type, "nav_status": r.nav_status,
                    "speed_kn": r.sog_kn, "heading": r.heading_deg,
                    "length_m": r.length_m, "width_m": r.width_m,
                    "draft_m": r.draft_m, "cargo": r.cargo,
                    "mmsi": r.mmsi,
                    "_lineage": "noaa_marine_cadastre_archive",
                },
                confidence=0.99,
                raw_lineage="noaa:marine_cadastre",
            )
            engine.ingest(obs)
            n_ingested += 1
        except Exception:  # noqa: BLE001
            n_skipped += 1
    return {"ingested": n_ingested, "skipped": n_skipped}
