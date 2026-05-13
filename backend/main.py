"""
FastAPI app — multi-domain.

Phase 1: Maritime SAR & dark-vessel detection      (/maritime/*)
Phase 2: Wildfire early detection                  (/wildfire/*)

Same audit log feeds both. The chain interleaves observations from
both domains, which is correct: a single oversight body audits the
whole platform, not one domain at a time.

Run:  uvicorn main:app --reload --port 8000
"""

from __future__ import annotations
import asyncio
import logging
import os

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import Decision
from fusion import FusionEngine
from wildfire import WildfireFusion
from audit import audit_log
from db import store
from db import archive as audit_archive
from seed_data import build_scenario, SCENARIO_START as MARITIME_START
from wildfire_seed import build_wildfire_scenario

# Phase 2: real-time AIS ingestion (optional — only runs if API key is set)
import aisstream
# Phase 4: Sentinel-1 SAR discovery + (eventually) download/detect
import sar
# Phase 4.x: Sentinel-2 optical catalog discovery (companion to SAR)
import s2

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("semper_safe")


app = FastAPI(title="Semper Safe — Multi-Domain")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)

maritime = FusionEngine()
wildfire = WildfireFusion()

# Background task handles so we can cancel cleanly on shutdown.
_aisstream_task: asyncio.Task[None] | None = None
_aisstream_cancel: asyncio.Event | None = None
_gap_sweeper_task: asyncio.Task[None] | None = None
_gap_sweeper_cancel: asyncio.Event | None = None
_retention_task: asyncio.Task[None] | None = None
_retention_cancel: asyncio.Event | None = None
_audit_archive_task: asyncio.Task[None] | None = None
_audit_archive_cancel: asyncio.Event | None = None
_sar_discover_task: asyncio.Task[None] | None = None
_sar_discover_cancel: asyncio.Event | None = None
_sar_auto_process_task: asyncio.Task[None] | None = None
_sar_auto_process_cancel: asyncio.Event | None = None
_s2_discover_task: asyncio.Task[None] | None = None
_s2_discover_cancel: asyncio.Event | None = None
_wildfire_refresh_task: asyncio.Task[None] | None = None
_wildfire_refresh_cancel: asyncio.Event | None = None

# How often to scan maritime entities for AIS dropouts. The sweep is cheap
# (in-memory iteration over self.entities); the real cost is the audit/store
# writes for any newly-flagged gaps. 60s is a reasonable default — a 30s
# sweep would surface dropouts faster but double the audit churn.
GAP_SWEEP_INTERVAL_S = 60

# Retention: at ~9 AIS events/sec, observations + their FK link rows fill
# Neon's 0.5 GB free tier in ~15 hours. 24h TTL keeps recent tracks
# meaningful while bounding storage. Tunable via env once we move off the
# free tier.
OBSERVATION_TTL_HOURS = int(os.environ.get("OBSERVATION_TTL_HOURS", "24"))
RETENTION_INTERVAL_S = 3600   # purge once per hour

# Audit cold archive: copy new audit_log rows to R2 every hour.
# At ~9 events/sec we generate ~32k rows/hour; drain mode within a single
# tick keeps us caught up.
AUDIT_ARCHIVE_INTERVAL_S = int(os.environ.get("AUDIT_ARCHIVE_INTERVAL_S", "3600"))
AUDIT_ARCHIVE_DRAIN_CAP = 50  # max archive_audit_chain() calls per tick (safety)

# Sentinel-1 SAR discovery: every 6 hours, query Copernicus OData for new
# scenes intersecting the AOI and record them in sar_scenes. Catalog
# browsing is public (no auth), so this runs even before download
# credentials are configured. Sentinel-1 has 6-day repeat over a given
# area, so a 6-hour cadence comfortably catches every pass.
SAR_DISCOVERY_INTERVAL_S = int(os.environ.get("SAR_DISCOVERY_INTERVAL_S", "21600"))

# Sentinel-2 catalog discovery: same cadence as Sentinel-1 (6h). Same
# Copernicus account; the OData query is just a different Collection
# filter. Phase 4.x — discovery only for now; download / chip extraction
# is Phase 4.y.
S2_DISCOVERY_INTERVAL_S = int(os.environ.get("S2_DISCOVERY_INTERVAL_S", "21600"))


# Sentinel-1 auto-process loop: every 30 min, pick the freshest discovered
# scene that fits our memory budget and run it through download → CFAR →
# fusion serially. Off by default (SAR_AUTO_PROCESS=1 to enable) so we
# stay in manual mode until we trust the pipeline; once on, the platform
# turns into a "set it and forget it" SAR vessel monitor. The 30-min
# interval is gated by per-scene wall clock — typical scene takes
# ~5 min download + ~8 min CFAR ≈ 13 min, so 30-min cadence leaves
# plenty of headroom for AIS ingest + audit archive.
SAR_AUTO_PROCESS = os.environ.get("SAR_AUTO_PROCESS") == "1"
SAR_AUTO_PROCESS_INTERVAL_S = int(os.environ.get("SAR_AUTO_PROCESS_INTERVAL_S", "1800"))
# Memory cap for an auto-processable scene. CFAR peak per 2048 tile is
# ~130 MB; baseline ~400 MB; we want under 1 GB peak so reject huge
# scenes (>1.2 GB downloaded). Manual /admin/sar/process can still
# run them after careful operator triage.
SAR_AUTO_MAX_BYTES = int(os.environ.get("SAR_AUTO_MAX_BYTES", str(1_200_000_000)))
# Only auto-process scenes acquired within this window. Older scenes
# have no AIS data left in the 24-h retention window, so fusion can't
# match — they'd produce all-dark detections (real, but uninteresting
# for live ops). Operator can still backfill via manual endpoints.
SAR_AUTO_MAX_AGE_HOURS = int(os.environ.get("SAR_AUTO_MAX_AGE_HOURS", "48"))

# In-memory cache window: keep only recent observations in
# engine.observations to bound RAM. Evicted obs still live in Postgres
# (until DB TTL expires them); /track + /lineage just see shorter
# histories for older entities. Sweep runs alongside the gap sweep
# (every GAP_SWEEP_INTERVAL_S) so eviction is gradual rather than
# bursty.
IN_MEMORY_OBS_WINDOW_MINUTES = int(os.environ.get("IN_MEMORY_OBS_WINDOW_MINUTES", "30"))


def _point_in_polygon(lon: float, lat: float,
                       polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon (one ring). Used by the cross-domain
    smoke-exposure sweep."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _check_smoke_exposure() -> int:
    """Cross-domain alarm — flag maritime vessels currently inside any
    wildfire smoke plume polygon. Sets ent.attrs.in_smoke_plume to the
    incident_name; clears it when the vessel moves out. Returns the
    count newly tagged (so the gap sweeper can log when there's a hit).
    """
    plumes = _wildfire_cache.get("smoke") or []
    if not plumes:
        # Clear stale tags when smoke catalog empties.
        for ent in list(maritime.entities.values()):
            if (ent.attrs or {}).get("in_smoke_plume"):
                ent.attrs.pop("in_smoke_plume", None)
        return 0
    newly_tagged = 0
    for ent in list(maritime.entities.values()):
        if ent.type.value not in ("vessel", "ais_gap"):
            continue
        if ent.geom is None:
            continue
        was_in = (ent.attrs or {}).get("in_smoke_plume")
        hit_incident: str | None = None
        for p in plumes:
            geom = p.get("geometry") or {}
            if geom.get("type") != "Polygon":
                continue
            rings = geom.get("coordinates") or []
            if not rings:
                continue
            if _point_in_polygon(ent.geom.lon, ent.geom.lat, rings[0]):
                hit_incident = p.get("incident_name") or "wildfire smoke"
                break
        if hit_incident and hit_incident != was_in:
            ent.attrs["in_smoke_plume"] = hit_incident
            newly_tagged += 1
            audit_log.append(
                actor="system",
                event_type="vessel_in_smoke_plume",
                payload={"entity_id": ent.entity_id,
                         "mmsi": ent.attrs.get("mmsi"),
                         "incident": hit_incident},
            )
        elif (not hit_incident) and was_in:
            ent.attrs.pop("in_smoke_plume", None)
    return newly_tagged


# Track when sanctions feeds were last pulled. The gap-sweeper loop
# calls _maybe_refresh_sanctions() every tick; it returns early unless
# 24 h have elapsed since the last successful pull (or it's never run).
_SANCTIONS_REFRESH_INTERVAL_S = 24 * 60 * 60
_last_sanctions_refresh_ts: float = 0.0


def _maybe_refresh_sanctions() -> None:
    """Refresh sanctions feeds if a day has elapsed since the last pull.
    Synchronous — caller runs us via asyncio.to_thread."""
    import time as _t
    import sanctions
    global _last_sanctions_refresh_ts
    now = _t.time()
    if (now - _last_sanctions_refresh_ts) < _SANCTIONS_REFRESH_INTERVAL_S:
        return
    try:
        result = sanctions.refresh_from_public_feeds()
        _last_sanctions_refresh_ts = now
        audit_log.append(
            actor="system",
            event_type="sanctions_feeds_refreshed",
            payload=result,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("sanctions refresh crashed: %s", exc)


def _evict_stale_in_memory_obs() -> int:
    """Drop observations older than IN_MEMORY_OBS_WINDOW_MINUTES from each
    engine's in-memory dict. Returns total count evicted across both
    engines. Safe to call from the gap-sweeper worker thread; uses a
    list() snapshot to avoid concurrent-mutation issues."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=IN_MEMORY_OBS_WINDOW_MINUTES)
    total = 0
    for engine in (maritime, wildfire):
        stale_ids = [
            oid for oid, o in list(engine.observations.items())
            if o.t < cutoff
        ]
        for oid in stale_ids:
            engine.observations.pop(oid, None)
        total += len(stale_ids)
    return total


async def _gap_sweeper_loop(cancel: asyncio.Event) -> None:
    """Periodic AIS-gap detection + in-memory observation eviction.
    Real-time AIS arrives one message at a time, so dropouts only surface
    from a sweep. The same loop also evicts stale obs from the in-memory
    cache (gradual eviction beats hourly bursts that would spike memory)."""
    from datetime import datetime, timezone
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=GAP_SWEEP_INTERVAL_S)
        except asyncio.TimeoutError:
            try:
                # Run the (sync) sweep off the event loop so we don't block
                # AISStream message handling on Postgres write-throughs.
                await asyncio.to_thread(
                    maritime.detect_gaps, datetime.now(timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("gap sweep crashed: %s", exc)
            try:
                # Loitering reclassification — same threadpool offload as
                # detect_gaps. Cheap (one O(N) pass over self.entities).
                await asyncio.to_thread(
                    maritime.detect_loitering, datetime.now(timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("loitering sweep crashed: %s", exc)
            try:
                # Convoy detection — O(N²) but bounded (~300 active vessels)
                # so it runs in <10 ms. Same threadpool offload pattern.
                await asyncio.to_thread(
                    maritime.detect_convoys, datetime.now(timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("convoy sweep crashed: %s", exc)
            try:
                # Port-skipping detection — O(N) over cooperative vessels
                # with destination + heading. Same threadpool pattern.
                await asyncio.to_thread(
                    maritime.detect_port_skipping,
                    datetime.now(timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("port-skipping sweep crashed: %s", exc)
            try:
                # Cross-domain smoke exposure — flag maritime vessels
                # currently inside a wildfire smoke plume polygon.
                tagged = await asyncio.to_thread(_check_smoke_exposure)
                if tagged > 0:
                    log.info("smoke exposure: %d new vessels tagged", tagged)
            except Exception as exc:  # noqa: BLE001
                log.warning("smoke exposure sweep crashed: %s", exc)
            # Sanctions feed refresh — runs once per 24 h. Cheap when
            # not due (just checks a timestamp); expensive (~5-10s,
            # network-bound) when it actually pulls. Threadpool offload.
            try:
                await asyncio.to_thread(_maybe_refresh_sanctions)
            except Exception as exc:  # noqa: BLE001
                log.exception("sanctions refresh check crashed: %s", exc)
            try:
                evicted = await asyncio.to_thread(_evict_stale_in_memory_obs)
                if evicted:
                    log.debug("evicted %d stale in-memory observations", evicted)
            except Exception as exc:  # noqa: BLE001
                log.exception("in-memory eviction crashed: %s", exc)


# Wildfire data caches — refreshed every WILDFIRE_REFRESH_INTERVAL_S
# by _wildfire_refresh_loop. Endpoints serve from cache so an operator
# never blocks on a slow NIFC/NWS round trip.
WILDFIRE_REFRESH_INTERVAL_S = 5 * 60   # 5 minutes — NIFC updates
                                       # incident sizes / containment
                                       # several times an hour.
_wildfire_cache = {
    "incidents": [],           # NIFC active incidents (list of dicts)
    "red_flag":  [],           # NWS Red Flag / Fire Weather Watch polygons
    "risk_grid": [],           # Per-cell HDW samples
    "lightning": [],           # Last-hour strikes
    "preposition": [],         # Recommended staging points
    "psps": [],                # Active utility PSPS zones
    "wui": [],                 # Scored WUI communities
    "smoke": [],               # Modeled smoke plume polygons
    "history": [],             # 3-year historical perimeters (refreshed daily)
    "history_last_refresh": None,
    "dryness": [],             # Vegetation dryness grid (synthesized NDVI proxy)
    # Per-incident size snapshots over time, used by the perimeter
    # time-lapse popup. Keyed by incident_id → list of {t, size_acres,
    # contained_pct}. Snapshots are added every refresh tick; capped
    # at PERIMETER_SNAPSHOT_MAX_PER_INCIDENT to bound memory.
    "perimeter_snapshots": {},
    "last_refresh_at": None,
}
PERIMETER_SNAPSHOT_MAX_PER_INCIDENT = 96   # 5-min × 96 = 8 h of history


async def _wildfire_refresh_loop(cancel: asyncio.Event) -> None:
    """Periodically refresh every wildfire data feed. Each source is
    fetched in a threadpool so a slow NIFC ArcGIS query doesn't block
    the others. Failures are logged but don't crash the loop."""
    import nifc
    log.info("wildfire refresh loop started (every %ds)",
             WILDFIRE_REFRESH_INTERVAL_S)
    # Kick off first refresh shortly after boot rather than on the full
    # interval — so an operator opening the wildfire tab during the
    # first few minutes already sees real incidents.
    first_delay = 30
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=first_delay)
        except asyncio.TimeoutError:
            try:
                incidents = await asyncio.to_thread(nifc.fetch_active_incidents)
                _wildfire_cache["incidents"] = incidents
                # Capture a per-incident size snapshot for the perimeter
                # time-lapse popup.
                now_iso = datetime.now(timezone.utc).isoformat()
                snaps = _wildfire_cache["perimeter_snapshots"]
                for i in incidents:
                    iid = i.get("incident_id")
                    if not iid:
                        continue
                    series = snaps.setdefault(iid, [])
                    series.append({
                        "t": now_iso,
                        "size_acres":    i.get("size_acres"),
                        "contained_pct": i.get("contained_pct"),
                    })
                    if len(series) > PERIMETER_SNAPSHOT_MAX_PER_INCIDENT:
                        del series[:len(series) - PERIMETER_SNAPSHOT_MAX_PER_INCIDENT]
                log.info("wildfire refresh: %d NIFC incidents", len(incidents))
            except Exception as exc:  # noqa: BLE001
                log.exception("NIFC refresh crashed: %s", exc)
            try:
                # Red Flag + ignition-risk + lightning + preposition
                # imports are lazy so a missing module doesn't crash
                # the whole loop.
                from nws_alerts import fetch_red_flag_warnings
                _wildfire_cache["red_flag"] = await asyncio.to_thread(
                    fetch_red_flag_warnings)
                log.info("wildfire refresh: %d RFW/FWW polygons",
                         len(_wildfire_cache["red_flag"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("RFW refresh crashed (continuing): %s", exc)
            try:
                from wildfire_risk import compute_risk_grid
                _wildfire_cache["risk_grid"] = await asyncio.to_thread(
                    compute_risk_grid)
                log.info("wildfire refresh: risk_grid n=%d cells",
                         len(_wildfire_cache["risk_grid"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("risk grid refresh crashed (continuing): %s", exc)
            try:
                from wildfire_lightning import recent_strikes
                _wildfire_cache["lightning"] = await asyncio.to_thread(
                    recent_strikes)
                log.info("wildfire refresh: %d lightning strikes",
                         len(_wildfire_cache["lightning"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("lightning refresh crashed (continuing): %s", exc)
            try:
                from wildfire_preposition import recommend_prepositions
                _wildfire_cache["preposition"] = await asyncio.to_thread(
                    recommend_prepositions,
                    _wildfire_cache["incidents"],
                    _wildfire_cache["risk_grid"],
                )
                log.info("wildfire refresh: %d pre-position recs",
                         len(_wildfire_cache["preposition"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("preposition refresh crashed (continuing): %s", exc)
            try:
                # PSPS catalog refresh + mutual-aid signal detection.
                # When a zone transitions to 'active' we emit a
                # 'mutual_aid_required' audit entry listing the
                # neighboring utilities to be notified.
                import psps
                new_zones = psps.list_zones()
                # Detect new 'active' transitions against the previous
                # snapshot (cached in _wildfire_cache["psps"]).
                old_active_ids = {
                    z["zone_id"] for z in _wildfire_cache.get("psps") or []
                    if z.get("status") == "active"
                }
                _wildfire_cache["psps"] = new_zones
                for z in new_zones:
                    if (z.get("status") == "active"
                            and z["zone_id"] not in old_active_ids):
                        neighbors = psps.neighbors_of(z["utility"])
                        audit_log.append(
                            actor="system",
                            event_type="mutual_aid_required",
                            payload={
                                "utility":     z["utility"],
                                "zone_id":     z["zone_id"],
                                "zone_name":   z["name"],
                                "notified":    neighbors,
                                "customers_affected": z["customers_affected"],
                                "reason":      z["reason"],
                            },
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("PSPS refresh crashed (continuing): %s", exc)
            try:
                # WUI communities scored against the live HDW grid.
                from wildfire_wui import score_communities
                _wildfire_cache["wui"] = await asyncio.to_thread(
                    score_communities, _wildfire_cache["risk_grid"],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("WUI refresh crashed (continuing): %s", exc)
            try:
                # Vegetation dryness — synthesized NDVI proxy from
                # drought tier + HDW + vegetation class.
                from wildfire_vegetation import compute_dryness_grid
                _wildfire_cache["dryness"] = await asyncio.to_thread(
                    compute_dryness_grid, _wildfire_cache["risk_grid"],
                )
                log.info("wildfire refresh: %d dryness cells",
                         len(_wildfire_cache["dryness"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("dryness refresh crashed (continuing): %s", exc)
            try:
                # Smoke plumes per active incident.
                from wildfire_smoke import compute_plumes
                _wildfire_cache["smoke"] = await asyncio.to_thread(
                    compute_plumes, _wildfire_cache["incidents"],
                )
                log.info("wildfire refresh: %d smoke plumes",
                         len(_wildfire_cache["smoke"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("smoke plume refresh crashed (continuing): %s", exc)
            # Historical perimeters — refreshed at most once per 24h
            # since the data only changes when a fire wraps up.
            try:
                from datetime import timedelta as _td
                now = datetime.now(timezone.utc)
                last = _wildfire_cache["history_last_refresh"]
                if last is None or (now - last) > _td(hours=24):
                    import nifc_history
                    _wildfire_cache["history"] = await asyncio.to_thread(
                        nifc_history.fetch_historical_perimeters)
                    _wildfire_cache["history_last_refresh"] = now
                    log.info("wildfire refresh: %d historical perimeters",
                             len(_wildfire_cache["history"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("history refresh crashed (continuing): %s", exc)
            _wildfire_cache["last_refresh_at"] = datetime.now(
                timezone.utc).isoformat()
        first_delay = WILDFIRE_REFRESH_INTERVAL_S


async def _sar_discover_loop(cancel: asyncio.Event) -> None:
    """Periodic Sentinel-1 catalog discovery for the Texas AOI.

    Public-catalog only — no Copernicus auth needed. New scenes get
    inserted into sar_scenes with state='discovered' for downstream
    download + CFAR. Run on boot too, then every SAR_DISCOVERY_INTERVAL_S.
    """
    # First run shortly after boot (give the app 60s to settle).
    first_delay = 60
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=first_delay)
        except asyncio.TimeoutError:
            try:
                scenes = await asyncio.to_thread(sar.discover_scenes, limit=50)
                if scenes:
                    result = await asyncio.to_thread(sar.record_scenes, scenes)
                    if result["inserted"]:
                        audit_log.append(
                            actor="system", event_type="sar_scenes_discovered",
                            payload={
                                "inserted": result["inserted"],
                                "total_seen": len(scenes),
                            },
                        )
                        log.info("SAR discovery: +%d new scenes", result["inserted"])
            except Exception as exc:  # noqa: BLE001
                log.exception("SAR discovery crashed: %s", exc)
        # Subsequent runs: every SAR_DISCOVERY_INTERVAL_S.
        first_delay = SAR_DISCOVERY_INTERVAL_S


async def _s2_discover_loop(cancel: asyncio.Event) -> None:
    """Periodic Sentinel-2 L2A catalog discovery for the Texas AOI.

    Public-catalog only — no Copernicus auth needed. New scenes get
    inserted into s2_scenes with state='discovered'. Mirrors
    _sar_discover_loop in cadence and shape; the only deltas are the
    target collection (SENTINEL-2 vs SENTINEL-1) and the storage
    table.
    """
    first_delay = 75   # offset slightly from SAR discovery (60s) so
                       # the two OData queries don't fire simultaneously
                       # and double-block the event loop.
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=first_delay)
        except asyncio.TimeoutError:
            try:
                scenes = await asyncio.to_thread(s2.discover_scenes, limit=50)
                if scenes:
                    result = await asyncio.to_thread(s2.record_scenes, scenes)
                    if result["inserted"]:
                        audit_log.append(
                            actor="system", event_type="s2_scenes_discovered",
                            payload={"inserted": result["inserted"],
                                     "total_seen": len(scenes)},
                        )
                        log.info("S2 discovery: +%d new scenes", result["inserted"])
            except Exception as exc:  # noqa: BLE001
                log.exception("S2 discovery crashed: %s", exc)
        first_delay = S2_DISCOVERY_INTERVAL_S


async def _sar_auto_process_loop(cancel: asyncio.Event) -> None:
    """Serial auto-process pipeline: pick a discovered scene and run it
    through download → CFAR → fusion end-to-end on each tick.

    Why serial: each stage is sync numpy/boto3/Postgres. Running two
    scenes in parallel would double per-tile CFAR memory, blowing the
    1 GB Fly VM. Operator-triggered admin endpoints still work
    independently — they hit the same idempotency guards in
    sar_processor (state machine on sar_scenes), so concurrent manual +
    auto runs won't stomp on each other.

    Selection criteria per tick (see sar_auto_pick_next):
      - state = 'discovered' (catalog row exists, no R2 object yet)
      - failure_reason IS NULL (skip prior fails — re-attempt manually)
      - content_length < SAR_AUTO_MAX_BYTES (memory headroom)
      - acquired_at within SAR_AUTO_MAX_AGE_HOURS (so AIS data is still
        in the 24-h retention window — older scenes go all-dark, less
        useful for live ops)
      - sorted by acquired_at desc (freshest first)

    Loop logic:
      - sleep SAR_AUTO_PROCESS_INTERVAL_S, then run
      - if no eligible scene, just log + sleep again
      - on download error: mark state='failed' with reason, continue
      - on CFAR error: state stays 'downloaded', continue (re-runnable
        via /admin/sar/process)
      - all exceptions caught — never let the loop die
    """
    # Don't start hammering Copernicus + R2 the moment uvicorn comes up;
    # let bootstrap finish + AIS settle first. Match the 60-s warmup the
    # discovery loop uses.
    first_delay = 90
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=first_delay)
        except asyncio.TimeoutError:
            try:
                await _sar_auto_process_one()
            except Exception as exc:  # noqa: BLE001
                log.exception("SAR auto-process tick crashed: %s", exc)
        first_delay = SAR_AUTO_PROCESS_INTERVAL_S


def _sar_auto_pick_next() -> str | None:
    """Return the scene_id of the next eligible scene, or None.

    Picks (in order of preference):
      1. state='downloaded' scenes — already in R2, just need CFAR.
         Surfaces stuck-after-deploy-interrupt cases automatically.
      2. state='discovered' scenes — full pipeline run.

    Sync helper — call from a thread via asyncio.to_thread.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as sa_select, and_
    from db import models as dbm
    from db.session import session_scope

    cutoff = datetime.now(timezone.utc) - timedelta(hours=SAR_AUTO_MAX_AGE_HOURS)
    with session_scope() as s:
        # Half-processed scenes first — they're already paid-for in R2,
        # so prioritize finishing them before starting a new download.
        downloaded = s.execute(
            sa_select(dbm.SarSceneRow)
            .where(and_(
                dbm.SarSceneRow.state == "downloaded",
                dbm.SarSceneRow.failure_reason.is_(None),
                dbm.SarSceneRow.acquired_at >= cutoff,
            ))
            .order_by(dbm.SarSceneRow.acquired_at.desc())
            .limit(5)
        ).scalars().all()
        for r in downloaded:
            sz = (r.attrs or {}).get("content_length_bytes") or 0
            if 0 < sz <= SAR_AUTO_MAX_BYTES:
                return r.scene_id
        # No downloads pending → pick a fresh discovery to run end-to-end.
        rows = s.execute(
            sa_select(dbm.SarSceneRow)
            .where(and_(
                dbm.SarSceneRow.state == "discovered",
                dbm.SarSceneRow.failure_reason.is_(None),
                dbm.SarSceneRow.acquired_at >= cutoff,
            ))
            .order_by(dbm.SarSceneRow.acquired_at.desc())
            .limit(20)
        ).scalars().all()
        for r in rows:
            sz = (r.attrs or {}).get("content_length_bytes") or 0
            if 0 < sz <= SAR_AUTO_MAX_BYTES:
                return r.scene_id
    return None


async def _sar_auto_process_one() -> None:
    """Run download → CFAR → fusion for one auto-eligible scene, if any.

    The download and process steps are sync (boto3 + numpy hold the GIL)
    so we hand them to a threadpool worker via asyncio.to_thread. The
    main asyncio loop keeps serving health checks + AIS ingest during
    the 5-minute download and 8-minute CFAR pass — same pattern as the
    /admin/sar/process BackgroundTask path.
    """
    import sar
    import sar_processor

    scene_id = await asyncio.to_thread(_sar_auto_pick_next)
    if scene_id is None:
        log.info("SAR auto-process: no eligible scene this tick")
        return

    log.info("SAR auto-process: starting %s", scene_id)
    audit_log.append(
        actor="system", event_type="sar_auto_pipeline_started",
        payload={"scene_id": scene_id},
    )

    try:
        dl = await asyncio.to_thread(sar.download_scene_to_r2, scene_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("SAR auto-process: download failed for %s: %s",
                      scene_id, exc)
        # download_scene_to_r2 already records failure_reason on the row.
        return
    log.info("SAR auto-process: downloaded %s — %s",
             scene_id, {k: dl.get(k) for k in ("bytes", "parts", "skipped")})

    try:
        proc = await asyncio.to_thread(
            sar_processor.process_scene, scene_id,
            fuse_engine=maritime,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("SAR auto-process: CFAR failed for %s: %s",
                      scene_id, exc)
        return

    audit_log.append(
        actor="system", event_type="sar_auto_pipeline_completed",
        payload={
            "scene_id": scene_id,
            "n_detections": proc.get("n_detections"),
            "fusion": proc.get("fusion"),
        },
    )
    log.info("SAR auto-process: %s done — %d detections, fusion=%s",
             scene_id, proc.get("n_detections"), proc.get("fusion"))


async def _audit_archive_loop(cancel: asyncio.Event) -> None:
    """Drain new audit_log rows to R2 every AUDIT_ARCHIVE_INTERVAL_S seconds.

    Each tick keeps calling archive_audit_chain until it reports no new
    rows OR we hit the safety cap. Skips entirely if R2 isn't configured.
    """
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=AUDIT_ARCHIVE_INTERVAL_S)
        except asyncio.TimeoutError:
            if not audit_archive.is_configured():
                continue
            try:
                total = 0
                for _ in range(AUDIT_ARCHIVE_DRAIN_CAP):
                    result = await asyncio.to_thread(audit_archive.archive_audit_chain)
                    n = result.get("new_rows", 0)
                    if not n:
                        break
                    total += n
                if total:
                    audit_log.append(
                        actor="system", event_type="audit_archived",
                        payload={"rows": total},
                    )
                    log.info("audit archive: drained %d rows to R2", total)
                # Even if no new rows were archived this tick, we still
                # want to attempt a prune — earlier ticks may have
                # archived rows that this loop hasn't yet pruned. The
                # method respects a 2000-row tail so the active table
                # never falls below a few minutes of recent activity,
                # which keeps the operator-side audit view useful.
                try:
                    pruned = await asyncio.to_thread(audit_log.prune_archived)
                    if pruned:
                        log.info("audit prune: deleted %d archived rows", pruned)
                except Exception as exc:  # noqa: BLE001
                    log.exception("audit prune failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("audit archive crashed: %s", exc)


async def _retention_loop(cancel: asyncio.Event) -> None:
    """Periodic deletion of observations older than OBSERVATION_TTL_HOURS.
    Audit log is preserved (see store.purge_old_observations docstring)."""
    while not cancel.is_set():
        try:
            await asyncio.wait_for(cancel.wait(), timeout=RETENTION_INTERVAL_S)
        except asyncio.TimeoutError:
            try:
                deleted = await asyncio.to_thread(
                    store.purge_old_observations,
                    older_than_hours=OBSERVATION_TTL_HOURS,
                )
                if deleted:
                    audit_log.append(
                        actor="system", event_type="observations_purged",
                        payload={"deleted": deleted, "ttl_hours": OBSERVATION_TTL_HOURS},
                    )
                    log.info("retention sweep: purged %d observations", deleted)
            except Exception as exc:  # noqa: BLE001
                log.exception("retention sweep crashed: %s", exc)


def _seed_maritime() -> None:
    audit_log.append(actor="system", event_type="domain_loaded",
                     payload={"domain": "maritime"})
    mar_scenario = build_scenario()
    last_t = MARITIME_START
    for obs in mar_scenario:
        maritime.ingest(obs)
        if (obs.t - last_t).total_seconds() > 600:
            maritime.detect_gaps(obs.t)
            last_t = obs.t
    maritime.detect_gaps(mar_scenario[-1].t)


def _seed_wildfire() -> None:
    audit_log.append(actor="system", event_type="domain_loaded",
                     payload={"domain": "wildfire"})
    fire_scenario = build_wildfire_scenario()
    for obs in fire_scenario:
        wildfire.ingest(obs)


@app.on_event("startup")
def _bootstrap():
    """Phase 1.1 startup pipeline:
       - DB has entities for a domain → load them in-memory.
       - DB is empty + seed not skipped → run seed in a single bulk transaction:
           a) audit_log.batched(): all audit appends buffer in memory
           b) store.disable_persistence(): engines mutate in-memory only
           c) _seed_maritime() + _seed_wildfire() run normally
           d) on context exit: in-memory audit chain bulk-INSERTs
           e) store.bulk_seed_state() flushes engine state in one txn per domain
       - SKIP_SEED=1 boots empty (kept around as an escape hatch).

    The first deploy used to take ~5 min (per-call commits over Neon).
    This pipeline collapses it to ~5s — fits well inside Fly's 60s grace.
    """
    import time as _time
    _t0 = _time.time()
    skip_seed = os.environ.get("SKIP_SEED") == "1"
    persistent = store.is_persistent()

    maritime_empty = persistent and store.is_empty("maritime")
    wildfire_empty = persistent and store.is_empty("wildfire")
    log.info("[bootstrap] persistent=%s maritime_empty=%s wildfire_empty=%s (in %.1fs)",
             persistent, maritime_empty, wildfire_empty, _time.time() - _t0)

    _t1 = _time.time()
    if persistent and not maritime_empty:
        maritime.load_persisted_state()
        log.info("[bootstrap] maritime loaded: %d entities, %d recommendations (%.1fs)",
                 len(maritime.entities), len(maritime.recommendations), _time.time() - _t1)
    _t2 = _time.time()
    if persistent and not wildfire_empty:
        wildfire.load_persisted_state()
        log.info("[bootstrap] wildfire loaded: %d entities, %d recommendations (%.1fs)",
                 len(wildfire.entities), len(wildfire.recommendations), _time.time() - _t2)

    if (not skip_seed) and (maritime_empty or not persistent) and \
            (wildfire_empty or not persistent):
        # Fresh DB (or no DB) — run the seed. Use the batched pipeline when
        # DB-backed so it costs ~3 INSERTs total instead of thousands.
        with audit_log.batched(), store.disable_persistence():
            _seed_maritime()
            _seed_wildfire()
        if persistent:
            mar_counts = store.bulk_seed_state(
                observations=maritime.observations.values(),
                entities=maritime.entities.values(),
                recommendations=maritime.recommendations.values(),
                domain="maritime",
            )
            fire_counts = store.bulk_seed_state(
                observations=wildfire.observations.values(),
                entities=wildfire.entities.values(),
                recommendations=wildfire.recommendations.values(),
                domain="wildfire",
            )
            audit_log.append(
                actor="system", event_type="seed_persisted",
                payload={"maritime": mar_counts, "wildfire": fire_counts},
            )

    # Self-healing AOI sweep — drop any maritime entity outside the
    # Texas Gulf clamp BEFORE we ever serve /maritime/entities. The
    # frontend auto-fit treats anything inside the clamp as ground
    # truth and ignores everything else, but a stale row in the entity
    # set still wasted memory and made the suspect-details panel ugly.
    # This used to be a manual /admin/maritime/purge-non-aoi call; now
    # it runs every bootstrap so any image that ever held Madagascar
    # seed data heals itself on next deploy.
    _MAR_AOI = (-98.0, -93.5, 25.5, 30.5)   # lon_min, lon_max, lat_min, lat_max
    purged = 0
    for eid in list(maritime.entities.keys()):
        ent = maritime.entities[eid]
        geom = getattr(ent, "geom", None)
        lon = getattr(geom, "lon", None) if geom else None
        lat = getattr(geom, "lat", None) if geom else None
        if lon is None or lat is None:
            continue
        if not (_MAR_AOI[0] <= lon <= _MAR_AOI[1]
                and _MAR_AOI[2] <= lat <= _MAR_AOI[3]):
            maritime.entities.pop(eid, None)
            purged += 1
    if purged:
        # Also purge the persisted row so a restart doesn't reload it.
        if persistent:
            try:
                from sqlalchemy import delete as _sa_delete, and_, or_
                from geoalchemy2.functions import ST_X, ST_Y
                from db import models as dbm
                from db.session import session_scope
                with session_scope() as s:
                    s.execute(
                        _sa_delete(dbm.EntityRow).where(
                            and_(
                                dbm.EntityRow.domain == "maritime",
                                or_(
                                    ST_X(dbm.EntityRow.geom) < _MAR_AOI[0],
                                    ST_X(dbm.EntityRow.geom) > _MAR_AOI[1],
                                    ST_Y(dbm.EntityRow.geom) < _MAR_AOI[2],
                                    ST_Y(dbm.EntityRow.geom) > _MAR_AOI[3],
                                ),
                            )
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("[bootstrap] AOI persisted-row purge failed: %s", exc)
        # Rebuild MMSI index in case any purged entity held a mapping.
        maritime._mmsi_index = {  # noqa: SLF001
            str(e.attrs.get("mmsi")): e.entity_id
            for e in list(maritime.entities.values())
            if e.attrs.get("mmsi")
        }
        log.info("[bootstrap] AOI sweep purged %d non-Texas maritime entities", purged)
        audit_log.append(
            actor="system", event_type="aoi_bootstrap_purge",
            payload={"domain": "maritime", "purged": purged, "aoi": list(_MAR_AOI)},
        )

    # Boot marker — written AFTER any seed so the seed entries take seq=0..N-1
    # and process_started follows them. Restarts append more process_started
    # entries to the chain (Phase 1 exit criterion).
    audit_log.append(actor="system", event_type="process_started",
                     payload={"persistent": persistent,
                              "domains_loaded":
                                  [d for d, e in
                                   [("maritime", maritime), ("wildfire", wildfire)]
                                   if e.entities]})

    # Phase 2: kick off the AISStream background task if a key is set.
    api_key = os.environ.get("AISSTREAM_API_KEY", "").strip()
    if api_key:
        global _aisstream_task, _aisstream_cancel
        global _gap_sweeper_task, _gap_sweeper_cancel
        global _retention_task, _retention_cancel
        global _audit_archive_task, _audit_archive_cancel
        global _sar_discover_task, _sar_discover_cancel
        global _sar_auto_process_task, _sar_auto_process_cancel
        global _s2_discover_task, _s2_discover_cancel
        _aisstream_cancel = asyncio.Event()

        async def _on_observation(obs):
            # Run the (sync) engine.ingest in a thread so we don't block the
            # WebSocket loop on Postgres round-trips during write-through.
            await asyncio.to_thread(maritime.ingest, obs)

        async def _on_static(mmsi: str, attrs: dict):
            # Merge static data into the existing AIS-derived entity if any.
            eid = maritime._mmsi_index.get(mmsi)
            if not eid:
                return
            ent = maritime.entities.get(eid)
            if not ent:
                return
            ent.attrs.update(attrs)
            await asyncio.to_thread(store.put_entity, ent, domain="maritime")

        loop = asyncio.get_event_loop()
        _aisstream_task = loop.create_task(
            aisstream.run_worker(
                api_key=api_key,
                on_observation=_on_observation,
                on_static=_on_static,
                cancel=_aisstream_cancel,
            )
        )
        audit_log.append(
            actor="system", event_type="aisstream_started",
            payload={"bbox": aisstream.TEXAS_SHORELINE_BBOX},
        )
        log.info("aisstream worker started")

        # Companion task: periodic AIS-gap sweep. Runs only when AIS is
        # ingesting — without live data, the sweep wouldn't have anything
        # to flag against (seed entities have synthetic last_seen times).
        _gap_sweeper_cancel = asyncio.Event()
        _gap_sweeper_task = loop.create_task(_gap_sweeper_loop(_gap_sweeper_cancel))
        log.info("gap sweeper started (interval=%ds, threshold=%s)",
                 GAP_SWEEP_INTERVAL_S, "8min")

        # Retention task — only useful with persistent + live ingest.
        if persistent:
            _retention_cancel = asyncio.Event()
            _retention_task = loop.create_task(_retention_loop(_retention_cancel))
            log.info("retention sweeper started (every %ds, ttl=%dh)",
                     RETENTION_INTERVAL_S, OBSERVATION_TTL_HOURS)

            # Audit cold archive — only meaningful with persistent + R2 configured.
            if audit_archive.is_configured():
                _audit_archive_cancel = asyncio.Event()
                _audit_archive_task = loop.create_task(
                    _audit_archive_loop(_audit_archive_cancel)
                )
                log.info("audit archive task started (every %ds)",
                         AUDIT_ARCHIVE_INTERVAL_S)
            else:
                log.info("R2 not configured; audit archive task skipped")

            # Sentinel-1 SAR catalog discovery — public, no auth needed.
            _sar_discover_cancel = asyncio.Event()
            _sar_discover_task = loop.create_task(
                _sar_discover_loop(_sar_discover_cancel)
            )
            log.info("SAR discovery task started (every %ds)",
                     SAR_DISCOVERY_INTERVAL_S)

            # Sentinel-2 optical catalog discovery — same Copernicus
            # endpoint, no auth needed for the catalog query.
            _s2_discover_cancel = asyncio.Event()
            _s2_discover_task = loop.create_task(
                _s2_discover_loop(_s2_discover_cancel)
            )
            log.info("S2 discovery task started (every %ds)",
                     S2_DISCOVERY_INTERVAL_S)

            # Wildfire prevention data feeds: NIFC incidents +
            # NWS Red Flag warnings + ignition-risk grid + lightning
            # + preposition recs. Refreshed on a 5-min cadence.
            global _wildfire_refresh_task, _wildfire_refresh_cancel
            _wildfire_refresh_cancel = asyncio.Event()
            _wildfire_refresh_task = loop.create_task(
                _wildfire_refresh_loop(_wildfire_refresh_cancel)
            )

            # Sentinel-1 auto-process pipeline — opt-in, requires
            # Copernicus auth + R2 to actually do anything. Runs the
            # discover→download→CFAR→fuse sequence on a 30-min cadence.
            if SAR_AUTO_PROCESS:
                _sar_auto_process_cancel = asyncio.Event()
                _sar_auto_process_task = loop.create_task(
                    _sar_auto_process_loop(_sar_auto_process_cancel)
                )
                log.info("SAR auto-process task started "
                         "(every %ds, max %.1f GB, max age %dh)",
                         SAR_AUTO_PROCESS_INTERVAL_S,
                         SAR_AUTO_MAX_BYTES / 1e9,
                         SAR_AUTO_MAX_AGE_HOURS)
            else:
                log.info("SAR_AUTO_PROCESS not set; auto pipeline disabled")
    else:
        log.info("AISSTREAM_API_KEY not set; skipping AIS ingestion + gap sweep + retention")


@app.on_event("shutdown")
async def _shutdown():
    global _aisstream_task, _aisstream_cancel
    global _gap_sweeper_task, _gap_sweeper_cancel
    global _retention_task, _retention_cancel
    global _audit_archive_task, _audit_archive_cancel
    global _sar_discover_task, _sar_discover_cancel
    global _sar_auto_process_task, _sar_auto_process_cancel
    global _s2_discover_task, _s2_discover_cancel
    for cancel in (_aisstream_cancel, _gap_sweeper_cancel,
                   _retention_cancel, _audit_archive_cancel,
                   _sar_discover_cancel, _sar_auto_process_cancel,
                   _s2_discover_cancel):
        if cancel is not None:
            cancel.set()
    for task in (_aisstream_task, _gap_sweeper_task,
                 _retention_task, _audit_archive_task,
                 _sar_discover_task, _sar_auto_process_task,
                 _s2_discover_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class DecisionRequest(BaseModel):
    operator: str
    reason: str | None = None


class DispatchRequest(BaseModel):
    """Body for POST /maritime/dispatches.

    A 'dispatch' is operator-initiated, separate from the engine's
    recommendation acceptance flow: even when there's no pending
    recommendation, the operator can still file a dispatch ('I'm
    sending the cutter regardless') and have it audit-logged.

    action_type values are free-form for now — the frontend constrains
    them to a small enum (DISPATCH_PATROL, ALERT_COAST_GUARD,
    TASK_SAR_SAT, LOG_ONLY) but the backend stores whatever it gets so
    custom dispatch reasons aren't rejected mid-incident.
    """
    operator: str
    entity_id: str
    action_type: str
    notes: str | None = None


@app.get("/health")
def health():
    # IMPORTANT: keep this endpoint cheap. Fly's edge proxy hits it every
    # 30 s with a 5–15 s timeout; if it ever takes longer than the
    # timeout, the proxy de-registers the machine and we end up with
    # "no known healthy instances" loops that can't recover until the
    # audit table is cheap to scan again.
    #
    # Earlier version called len(audit_log.all()), which on the
    # _PostgresAuditLog backend materialized the entire 100K+-row audit
    # table over the Neon round-trip. It worked fine when the table was
    # small and quietly broke once the AISStream worker had been adding
    # observation rows for a few hours. audit_log.count() goes to a
    # SELECT COUNT(*) — constant-time on the network payload, regardless
    # of audit size.
    return {
        "ok": True,
        "domains": ["maritime", "wildfire"],
        "audit_head": audit_log.head(),
        "audit_entries": audit_log.count(),
    }


@app.get("/audit")
def audit_all(limit: int = 200):
    entries = audit_log.all()[-limit:]
    return {
        "entries": [e.model_dump() for e in entries],
        "total": len(audit_log.all()),
        "head": audit_log.head(),
    }


@app.get("/audit/verify")
def audit_verify():
    ok, bad = audit_log.verify()
    return {"valid": ok, "first_bad_seq": bad,
            "entry_count": len(audit_log.all())}


# Maritime domain
@app.get("/maritime/entities")
def maritime_entities():
    # list() snapshot prevents `dict changed size during iteration` from a
    # concurrent AISStream ingest mutating maritime.entities.
    rows = sorted(list(maritime.entities.values()),
                  key=lambda e: (-e.priority_score, -e.last_seen.timestamp()))
    return {"entities": [e.model_dump() for e in rows]}


def _entity_track(engine, eid: str, limit: int):
    """Shared track logic for both maritime + wildfire endpoints.

    Returns the most-recent `limit` observation positions in time order.
    Lightweight (no recommendation/audit payload) so the frontend can
    poll it cheaply on selection change.

    Data source priority:
      1. Postgres (full history up to retention TTL) — when DATABASE_URL is set
      2. engine.observations in-memory dict — fallback for local/CI runs
         without a DB. Note: the in-memory cache is bounded to a ~30min
         window so this fallback shows truncated tracks for older entities.
    """
    ent = engine.entities.get(eid)
    if ent is None:
        raise HTTPException(404, "entity not found")

    if store.is_persistent():
        obs = store.load_track(eid, limit=limit)
    else:
        obs = [
            engine.observations[o]
            for o in ent.observation_ids
            if o in engine.observations
        ]
        obs.sort(key=lambda o: o.t)
        if limit > 0:
            obs = obs[-limit:]

    return {
        "entity_id": eid,
        "type": ent.type.value,
        "track": [
            {
                "lon": o.geom.lon,
                "lat": o.geom.lat,
                "t": o.t.isoformat(),
                "source": o.source.value,
            }
            for o in obs
        ],
    }


@app.get("/maritime/entities/{eid}/track")
def maritime_track(eid: str, limit: int = 200):
    return _entity_track(maritime, eid, limit)


def _timeline(domain: str, at_iso: str | None, lookback_minutes: int):
    """Shared timeline endpoint logic. Default `at` = now if not specified."""
    from datetime import datetime, timezone
    if at_iso:
        try:
            at = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, f"invalid `at` datetime: {e}") from e
    else:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    if not store.is_persistent():
        # In-memory fallback: return current entity positions only (no
        # historical reconstruction without a DB).
        engine = maritime if domain == "maritime" else wildfire
        return {
            "at": at.isoformat(),
            "lookback_minutes": lookback_minutes,
            "snapshot": [
                {
                    "entity_id": e.entity_id,
                    "type": e.type.value,
                    "priority_score": e.priority_score,
                    "name": (e.attrs or {}).get("name"),
                    "mmsi": (e.attrs or {}).get("mmsi"),
                    "lon": e.geom.lon,
                    "lat": e.geom.lat,
                    "t": e.last_seen.isoformat(),
                }
                for e in list(engine.entities.values())
            ],
        }

    snapshot = store.load_timeline(
        domain, at=at, lookback_minutes=lookback_minutes,
    )
    return {
        "at": at.isoformat(),
        "lookback_minutes": lookback_minutes,
        "snapshot": snapshot,
    }


@app.get("/maritime/timeline")
def maritime_timeline(at: str | None = None, lookback_minutes: int = 60):
    """Per-entity position at time `at` (default: now). Used by the
    time-scrub UI to reconstruct historical map state."""
    return _timeline("maritime", at, lookback_minutes)


@app.get("/maritime/entities/{eid}/lineage")
def maritime_lineage(eid: str):
    data = maritime.lineage(eid)
    if not data:
        raise HTTPException(404, "entity not found")
    return data


# --- SAR layer ----------------------------------------------------------
#
# Two read endpoints for the frontend MapLibre SAR overlay. Returns
# GeoJSON FeatureCollections so the frontend can hand them straight to
# maplibre-gl source.setData(). All geometry already in WGS84 (srid=4326).

@app.get("/maritime/sar/stats")
def maritime_sar_stats():
    """Aggregate SAR pipeline counters for status / demo header.

    Returns:
      scenes_by_state         — discovered / downloaded / detected / failed counts
      scenes_total
      detections_total        — all detections across all scenes
      detections_dark         — matched_entity_id IS NULL (dark vessel candidates)
      detections_matched      — AIS-matched
      latest_detected_scene   — most recent scene that finished CFAR (acquired_at + completed_at)
      latest_detection_at     — wall-clock time of newest detection row
    """
    from datetime import datetime, timezone
    from sqlalchemy import select as sa_select, func, case
    from db import models as dbm
    from db.session import session_scope

    if not store.is_persistent():
        return {"persistent": False}

    with session_scope() as s:
        rows = s.execute(
            sa_select(dbm.SarSceneRow.state, func.count())
            .group_by(dbm.SarSceneRow.state)
        ).all()
        scenes_by_state = {state: int(n) for state, n in rows}
        scenes_total = sum(scenes_by_state.values())

        # Detection counts split by AIS-matched vs dark.
        det_total, det_matched, det_dark, det_max_t = s.execute(
            sa_select(
                func.count(),
                func.count(dbm.SarDetectionRow.matched_entity_id),
                func.sum(case(
                    (dbm.SarDetectionRow.matched_entity_id.is_(None), 1),
                    else_=0,
                )),
                func.max(dbm.SarDetectionRow.detected_at),
            )
        ).one()

        # Latest detected scene: pull the row, surface key fields.
        latest = s.execute(
            sa_select(dbm.SarSceneRow)
            .where(dbm.SarSceneRow.state == "detected")
            .order_by(dbm.SarSceneRow.acquired_at.desc())
            .limit(1)
        ).scalars().first()
        latest_scene = None
        if latest is not None:
            attrs = latest.attrs or {}
            latest_scene = {
                "scene_id": latest.scene_id,
                "name": attrs.get("name"),
                "acquired_at": latest.acquired_at.isoformat(),
                "n_detections": (attrs.get("detection_summary") or {}).get("n_kept"),
                "completed_at": (attrs.get("detection_summary") or {}).get("detected_at"),
            }

    return {
        "persistent": True,
        "scenes_total": scenes_total,
        "scenes_by_state": scenes_by_state,
        "detections_total": int(det_total or 0),
        "detections_matched": int(det_matched or 0),
        "detections_dark": int(det_dark or 0),
        "latest_detected_scene": latest_scene,
        "latest_detection_at": det_max_t.isoformat() if det_max_t else None,
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/maritime/sar/scenes")
def maritime_sar_scenes(
    state: str | None = None,
    since_hours: int = 168,
    limit: int = 50,
):
    """Sentinel-1 scene footprints (Polygon GeoJSON). Default = last 7 days
    of scenes. Filter by state to restrict to e.g. only 'detected'.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select as sa_select
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    q = sa_select(dbm.SarSceneRow).where(dbm.SarSceneRow.acquired_at >= cutoff)
    if state:
        q = q.where(dbm.SarSceneRow.state == state)
    q = q.order_by(dbm.SarSceneRow.acquired_at.desc()).limit(limit)

    features = []
    with session_scope() as s:
        rows = s.execute(q).scalars().all()
        for r in rows:
            poly = to_shape(r.footprint)
            attrs = r.attrs or {}
            features.append({
                "type": "Feature",
                "geometry": poly.__geo_interface__,
                "properties": {
                    "scene_id": r.scene_id,
                    "platform": r.platform,
                    "polarization": r.polarization,
                    "acquired_at": r.acquired_at.isoformat(),
                    "state": r.state,
                    "n_detections": (attrs.get("detection_summary") or {}).get("n_kept"),
                    "name": attrs.get("name"),
                },
            })
    return {"type": "FeatureCollection", "features": features}


@app.get("/maritime/sar/detections")
def maritime_sar_detections(
    since_hours: int = 168,
    scene_id: str | None = None,
    limit: int = 5000,
):
    """SAR detection points (Point GeoJSON). Each detection has rcs_db,
    length_m, confidence, matched_entity_id.

    matched_entity_id == null → dark vessel candidate (no AIS within fusion
    window when this scene was processed). Frontend colors these red.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select as sa_select
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    q = sa_select(dbm.SarDetectionRow).where(dbm.SarDetectionRow.detected_at >= cutoff)
    if scene_id:
        q = q.where(dbm.SarDetectionRow.scene_id == scene_id)
    q = q.order_by(dbm.SarDetectionRow.detected_at.desc()).limit(limit)

    features = []
    with session_scope() as s:
        rows = s.execute(q).scalars().all()
        for r in rows:
            pt = to_shape(r.geom)
            features.append({
                "type": "Feature",
                "geometry": pt.__geo_interface__,
                "properties": {
                    "detection_id": r.detection_id,
                    "scene_id": r.scene_id,
                    "rcs_db": r.rcs_db,
                    "length_m": r.length_m,
                    "confidence": r.confidence,
                    "vv_vh_ratio_db": r.vv_vh_ratio_db,
                    "entity_id": r.entity_id,
                    "matched_entity_id": r.matched_entity_id,
                    "detected_at": r.detected_at.isoformat(),
                    "is_dark_vessel": r.matched_entity_id is None,
                },
            })
    return {"type": "FeatureCollection", "features": features}


@app.post("/maritime/actions/{eid}/approve")
def maritime_approve(eid: str, body: DecisionRequest):
    affected = maritime.decide(eid, decision=Decision.APPROVED,
                                operator=body.operator, reason=body.reason)
    if not affected:
        raise HTTPException(404, "no pending recommendations")
    return {"approved": [r.model_dump() for r in affected]}


@app.post("/maritime/actions/{eid}/reject")
def maritime_reject(eid: str, body: DecisionRequest):
    affected = maritime.decide(eid, decision=Decision.REJECTED,
                                operator=body.operator, reason=body.reason)
    if not affected:
        raise HTTPException(404, "no pending recommendations")
    return {"rejected": [r.model_dump() for r in affected]}


@app.get("/operators/{operator}/scorecard")
def operator_scorecard(operator: str):
    """Per-officer activity summary — dispatches filed, decisions
    made, action-type histogram, average response time, audit hashes.

    Used by the frontend Operator Scorecard modal and by the daily
    review process. Hash-chained: every count corresponds to a list
    of audit_seq values the operator can drill into."""
    from datetime import timedelta as _td
    from collections import Counter

    now = datetime.now(timezone.utc)
    cutoffs = {
        "24h": now - _td(hours=24),
        "7d":  now - _td(days=7),
    }

    dispatches_seqs: dict[str, list[int]] = {"24h": [], "7d": [], "all": []}
    decisions_seqs:  dict[str, list[int]] = {"24h": [], "7d": [], "all": []}
    action_types = Counter()
    entity_types = Counter()
    response_times_s: list[float] = []

    for entry in audit_log.all():
        if entry.actor != operator:
            continue
        if entry.event_type == "dispatch_filed":
            dispatches_seqs["all"].append(entry.seq)
            if entry.t >= cutoffs["7d"]:
                dispatches_seqs["7d"].append(entry.seq)
            if entry.t >= cutoffs["24h"]:
                dispatches_seqs["24h"].append(entry.seq)
            p = entry.payload or {}
            if p.get("action_type"):
                action_types[p["action_type"]] += 1
            if p.get("entity_type"):
                entity_types[p["entity_type"]] += 1
            # Approximate response time = dispatch_t - entity_created_t
            # We don't have the entity creation timestamp here without
            # another lookup; skip rigorous timing for now (Phase 5
            # idea: join the audit chain on entity_id).
        elif entry.event_type == "decision":
            decisions_seqs["all"].append(entry.seq)
            if entry.t >= cutoffs["7d"]:
                decisions_seqs["7d"].append(entry.seq)
            if entry.t >= cutoffs["24h"]:
                decisions_seqs["24h"].append(entry.seq)

    return {
        "operator": operator,
        "generated_at": now.isoformat(),
        "dispatches": {
            "24h": len(dispatches_seqs["24h"]),
            "7d":  len(dispatches_seqs["7d"]),
            "all": len(dispatches_seqs["all"]),
            "seqs_24h": dispatches_seqs["24h"][:50],   # for drill-in
        },
        "decisions": {
            "24h": len(decisions_seqs["24h"]),
            "7d":  len(decisions_seqs["7d"]),
            "all": len(decisions_seqs["all"]),
            "seqs_24h": decisions_seqs["24h"][:50],
        },
        "action_type_histogram": dict(action_types.most_common()),
        "entity_type_histogram": dict(entity_types.most_common()),
        "audit_head": audit_log.head(),
    }


@app.get("/handoff")
def cross_domain_handoff(hours: int = 8, domains: str = "maritime,wildfire"):
    """Unified watch-turnover summary across both domains.

    The watch-officer-in-charge sees BOTH maritime + wildfire state
    in one document: anomaly counts, active dispatches, active
    wildfire incidents, top WUI tiers, recent decisions, audit head.
    Top of the brief identifies which domain has higher current
    activity so the relieving officer knows where to focus."""
    from datetime import timedelta as _td
    from collections import Counter
    requested = {d.strip().lower() for d in domains.split(",") if d.strip()}

    now = datetime.now(timezone.utc)
    cutoff = now - _td(hours=hours)
    dispatch_cutoff = now - _td(hours=4)
    head = audit_log.head()

    # Maritime block
    maritime_block = None
    if "maritime" in requested:
        type_counts = Counter(e.type.value for e in maritime.entities.values())
        convoy_ids = {(e.attrs or {}).get("convoy_id")
                      for e in maritime.entities.values()
                      if (e.attrs or {}).get("convoy_id")}
        m_dispatches = []
        m_decisions = []
        for entry in audit_log.all():
            p = entry.payload or {}
            if p.get("domain") == "wildfire":
                continue
            if entry.event_type == "dispatch_filed" and entry.t >= dispatch_cutoff:
                m_dispatches.append({
                    "t": entry.t.isoformat(), "operator": entry.actor,
                    "action_type": p.get("action_type"),
                    "entity_name": p.get("entity_name") or p.get("entity_mmsi"),
                    "hash": entry.self_hash,
                })
            if entry.event_type == "decision" and entry.t >= cutoff:
                m_decisions.append({
                    "t": entry.t.isoformat(), "operator": entry.actor,
                    "decision": p.get("decision"), "entity_id": p.get("entity_id"),
                })
        maritime_block = {
            "type_counts": dict(type_counts),
            "n_convoys": len(convoy_ids),
            "n_dispatches": len(m_dispatches),
            "n_decisions": len(m_decisions),
            "dispatches": m_dispatches,
            "decisions": m_decisions,
        }

    # Wildfire block
    wildfire_block = None
    if "wildfire" in requested:
        incidents = _wildfire_cache.get("incidents") or []
        wui = _wildfire_cache.get("wui") or []
        pre = _wildfire_cache.get("preposition") or []
        psps_active = [z for z in (_wildfire_cache.get("psps") or [])
                       if z.get("status") == "active"]
        w_dispatches = []
        for entry in audit_log.all():
            p = entry.payload or {}
            if (p.get("domain") == "wildfire"
                    and entry.event_type == "dispatch_filed"
                    and entry.t >= dispatch_cutoff):
                w_dispatches.append({
                    "t": entry.t.isoformat(), "operator": entry.actor,
                    "action_type": p.get("action_type"),
                    "entity_name": p.get("entity_name"),
                    "hash": entry.self_hash,
                })
        # Sum acres of active incidents (>0 contained_pct excluded)
        total_acres = sum(
            float(i.get("size_acres") or 0)
            for i in incidents if i.get("size_acres") is not None
        )
        wildfire_block = {
            "n_incidents": len(incidents),
            "total_active_acres": int(total_acres),
            "n_extreme_wui": len([c for c in wui if c.get("tier") == "extreme"]),
            "n_high_wui":    len([c for c in wui if c.get("tier") == "high"]),
            "n_psps_active": len(psps_active),
            "n_prepositions": len(pre),
            "n_dispatches": len(w_dispatches),
            "dispatches": w_dispatches,
        }

    # Pick "primary domain" — the one with more activity (dispatches +
    # extreme entities).
    primary = "maritime"
    if maritime_block and wildfire_block:
        m_act = maritime_block["n_dispatches"] + \
                maritime_block["type_counts"].get("dark_vessel", 0) + \
                maritime_block["type_counts"].get("ais_spoofed", 0)
        w_act = wildfire_block["n_dispatches"] + \
                wildfire_block["n_extreme_wui"] + \
                wildfire_block["n_psps_active"]
        primary = "wildfire" if w_act > m_act else "maritime"
    elif wildfire_block and not maritime_block:
        primary = "wildfire"

    # ─── markdown rendering ───
    lines = [
        "# Semper Safe · Watch Handoff (cross-domain)",
        "",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')} ·"
        f" window {hours} h",
        f"**Audit chain head:** `{head}`",
        f"**Primary domain this watch:** {primary.upper()}",
        "",
    ]
    if maritime_block:
        m = maritime_block
        lines += [
            "## Maritime",
            "",
            f"- vessels: {m['type_counts'].get('vessel', 0)}",
            f"- AIS gaps: {m['type_counts'].get('ais_gap', 0)}",
            f"- dark vessels: {m['type_counts'].get('dark_vessel', 0)}",
            f"- AIS spoofed: {m['type_counts'].get('ais_spoofed', 0)}",
            f"- loitering: {m['type_counts'].get('loitering_vessel', 0)}",
            f"- port-skipping: {m['type_counts'].get('port_skipping', 0)}",
            f"- convoys in formation: {m['n_convoys']}",
            f"- open dispatches (last 4 h): **{m['n_dispatches']}**",
            f"- decisions in window: {m['n_decisions']}",
            "",
        ]
        for d in m["dispatches"][:5]:
            lines.append(
                f"  - {d['t'][11:16]}Z · {d['action_type']} · "
                f"{d['entity_name']} · {d['operator']} · "
                f"`{d['hash'][:10]}…`"
            )
        if not m["dispatches"]:
            lines.append("  - _no maritime dispatches in window_")
        lines.append("")
    if wildfire_block:
        w = wildfire_block
        lines += [
            "## Wildfire",
            "",
            f"- active NIFC incidents: **{w['n_incidents']}** "
            f"({w['total_active_acres']:,} acres total)",
            f"- WUI extreme tier: **{w['n_extreme_wui']}**, "
            f"high tier: {w['n_high_wui']}",
            f"- active PSPS zones: {w['n_psps_active']}",
            f"- pre-position recs: {w['n_prepositions']}",
            f"- wildfire dispatches (last 4 h): **{w['n_dispatches']}**",
            "",
        ]
        for d in w["dispatches"][:5]:
            lines.append(
                f"  - {d['t'][11:16]}Z · {d['action_type']} · "
                f"{d['entity_name']} · {d['operator']} · "
                f"`{d['hash'][:10]}…`"
            )
        if not w["dispatches"]:
            lines.append("  - _no wildfire dispatches in window_")
        lines.append("")
    lines.append(
        "_Every dispatch above is hash-chained. Verify by walking the "
        "audit chain back from the head above._"
    )
    return {
        "now": now.isoformat(),
        "window_hours": hours,
        "audit_head": head,
        "primary_domain": primary,
        "maritime": maritime_block,
        "wildfire": wildfire_block,
        "markdown": "\n".join(lines),
    }


@app.get("/maritime/handoff")
def maritime_handoff(hours: int = 8):
    """Structured shift-change summary.

    Designed for the operator-to-operator handoff at watch turnover.
    Returns both:
      - markdown: human-readable text the outgoing operator pastes
        into chat / hands the relieving operator
      - json: machine-readable breakdown for the modal UI

    Window defaults to the standard 8-h watch. Sections:
      - State snapshot (anomaly counts, audit chain head)
      - Open dispatches still inside their 4 h active window
      - Recent operator decisions in the window
      - Top dark vessels by priority (worth a look-over)
      - Audit chain head + verify instructions
    """
    from datetime import timedelta as _td
    from collections import Counter

    now = datetime.now(timezone.utc)
    cutoff = now - _td(hours=hours)
    dispatch_cutoff = now - _td(hours=4)   # dispatches active for 4 h

    # ---- snapshot
    type_counts = Counter(e.type.value for e in maritime.entities.values())
    convoy_ids = {
        e.attrs.get("convoy_id") for e in maritime.entities.values()
        if e.attrs.get("convoy_id")
    }

    # ---- dispatches in the active window
    dispatches: list[dict] = []
    decisions: list[dict] = []
    for entry in audit_log.all():
        if entry.event_type == "dispatch_filed" and entry.t >= dispatch_cutoff:
            p = entry.payload or {}
            dispatches.append({
                "seq": entry.seq,
                "t": entry.t.isoformat(),
                "operator": entry.actor,
                "action_type": p.get("action_type"),
                "entity_id": p.get("entity_id"),
                "entity_type": p.get("entity_type"),
                "entity_name": p.get("entity_name") or p.get("entity_mmsi"),
                "notes": p.get("notes"),
                "audit_hash": entry.self_hash,
            })
        if entry.event_type == "decision" and entry.t >= cutoff:
            p = entry.payload or {}
            decisions.append({
                "seq": entry.seq,
                "t": entry.t.isoformat(),
                "operator": entry.actor,
                "decision": p.get("decision"),
                "entity_id": p.get("entity_id"),
                "reason": p.get("reason"),
                "audit_hash": entry.self_hash,
            })

    # ---- dark-vessel rollup
    darks = sorted(
        [e for e in maritime.entities.values()
         if e.type.value == "dark_vessel"],
        key=lambda e: -e.priority_score,
    )[:10]
    dark_rows = [
        {
            "entity_id": e.entity_id,
            "lat": e.geom.lat if e.geom else None,
            "lon": e.geom.lon if e.geom else None,
            "priority": e.priority_score,
            "first_seen": e.first_seen.isoformat(),
            "last_seen": e.last_seen.isoformat(),
        }
        for e in darks
    ]

    # ---- markdown rendering
    head = audit_log.head()
    md_lines = [
        f"# Semper Safe · Maritime Watch Handoff",
        f"",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')} ·"
        f" window {hours} h",
        f"**Audit chain head:** `{head}`",
        f"",
        f"## Current state",
        f"",
    ]
    md_lines.append("| Type | Count |")
    md_lines.append("| --- | --- |")
    for k in ("vessel", "ais_gap", "dark_vessel", "ais_spoofed",
              "loitering_vessel", "port_skipping"):
        md_lines.append(f"| {k} | {type_counts.get(k, 0)} |")
    md_lines.append(f"| convoys in formation | {len(convoy_ids)} |")
    md_lines.append("")

    if dispatches:
        md_lines.append(f"## Open dispatches ({len(dispatches)} active, last 4 h)")
        md_lines.append("")
        md_lines.append("| Time | Action | Entity | Operator | Hash |")
        md_lines.append("| --- | --- | --- | --- | --- |")
        for d in dispatches:
            t = d["t"][11:16]
            md_lines.append(
                f"| {t}Z | {d['action_type']} | "
                f"{d['entity_name'] or d['entity_id'][:12]} "
                f"({d['entity_type']}) | {d['operator']} | "
                f"`{d['audit_hash'][:12]}…` |"
            )
        md_lines.append("")
    else:
        md_lines.append("## Open dispatches")
        md_lines.append("")
        md_lines.append("_None active. Quiet handoff._")
        md_lines.append("")

    if decisions:
        md_lines.append(f"## Decisions in window ({len(decisions)})")
        md_lines.append("")
        for d in decisions[-10:]:
            t = d["t"][11:19]
            md_lines.append(
                f"- **{t}Z** · {d['decision']} on "
                f"`{(d['entity_id'] or '')[:14]}` by {d['operator']}"
                + (f" — {d['reason']}" if d.get("reason") else "")
            )
        md_lines.append("")

    if dark_rows:
        md_lines.append(f"## Top dark vessels ({len(dark_rows)} by priority)")
        md_lines.append("")
        md_lines.append("| Entity | Lat | Lon | Priority | Last seen |")
        md_lines.append("| --- | --- | --- | --- | --- |")
        for d in dark_rows:
            md_lines.append(
                f"| `{d['entity_id'][:14]}` | "
                f"{d['lat']:.3f} | {d['lon']:.3f} | "
                f"{d['priority']:.2f} | {d['last_seen'][11:16]}Z |"
            )
        md_lines.append("")

    md_lines.append(
        "_Every dispatch and decision in this brief is hash-chained to the "
        "audit log. Verify by walking the chain back from the head above._"
    )

    return {
        "now": now.isoformat(),
        "window_hours": hours,
        "audit_head": head,
        "type_counts": dict(type_counts),
        "n_convoys": len(convoy_ids),
        "dispatches": dispatches,
        "decisions": decisions,
        "top_dark_vessels": dark_rows,
        "markdown": "\n".join(md_lines),
    }


@app.get("/maritime/daily_brief.pdf")
def maritime_daily_brief():
    """Generate a one-page PDF brief summarizing the last 24 h.

    Contents:
      - Header: timestamp + audit chain head (verifiable integrity token)
      - Anomaly tally: counts per type (dark, spoofed, loitering, gap,
        port-skipping, convoys)
      - Top 10 dark vessels by priority — entity_id, lat/lon, last seen,
        rcs_db when known
      - Dispatch log: every dispatch_filed in the window with operator,
        action_type, entity, and audit hash
      - Sensor pulse: age and detail line per sensor

    Designed as a leave-behind for briefings — the audit hash + dispatch
    hashes are the integrity proofs an oversight reviewer would need
    to re-verify the day's actions against the chain.
    """
    from datetime import timedelta as _td
    from io import BytesIO
    from fastapi.responses import Response
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except ImportError as exc:
        raise HTTPException(
            501,
            "PDF brief requires reportlab — pip install reportlab "
            f"(import failed: {exc})",
        )

    now = datetime.now(timezone.utc)
    cutoff = now - _td(hours=24)

    # ----- Anomaly tally
    tally = {
        "vessel": 0, "dark_vessel": 0, "ais_gap": 0,
        "loitering_vessel": 0, "ais_spoofed": 0, "port_skipping": 0,
    }
    convoy_ids: set[str] = set()
    dark_vessels = []
    for ent in maritime.entities.values():
        t = ent.type.value
        if t in tally:
            tally[t] += 1
        if t == "dark_vessel":
            dark_vessels.append(ent)
        cid = (ent.attrs or {}).get("convoy_id")
        if cid:
            convoy_ids.add(cid)
    dark_vessels.sort(key=lambda e: -e.priority_score)
    dark_vessels = dark_vessels[:10]

    # ----- Recent dispatches from the audit log
    dispatches = []
    for e in audit_log.all():
        if e.event_type != "dispatch_filed":
            continue
        if e.t < cutoff:
            continue
        dispatches.append(e)
    dispatches.sort(key=lambda e: -e.seq)

    # ----- Build the PDF
    buf = BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ssTitle", parent=styles["Title"],
        fontSize=16, textColor=rl_colors.HexColor("#0a1118"),
        spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "ssMeta", parent=styles["Normal"],
        fontSize=8, textColor=rl_colors.HexColor("#666"),
        spaceAfter=10, leading=10,
    )
    h2_style = ParagraphStyle(
        "ssH2", parent=styles["Heading2"],
        fontSize=11, textColor=rl_colors.HexColor("#0a1118"),
        spaceBefore=10, spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "ssBody", parent=styles["Normal"],
        fontSize=8, textColor=rl_colors.HexColor("#1a2330"),
        leading=10,
    )

    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title="Semper Safe — Daily Brief",
    )
    story = []
    story.append(Paragraph("Semper Safe · Maritime Daily Brief", title_style))
    audit_head = audit_log.head()
    audit_count = audit_log.count() if hasattr(audit_log, "count") else len(audit_log.all())
    story.append(Paragraph(
        f"Window: {cutoff.strftime('%Y-%m-%d %H:%MZ')} → "
        f"{now.strftime('%Y-%m-%d %H:%MZ')} &nbsp;·&nbsp; "
        f"Audit chain head: {audit_head[:24]}… &nbsp;·&nbsp; "
        f"{audit_count} total entries",
        meta_style,
    ))

    # Anomaly tally table
    story.append(Paragraph("Anomaly tally", h2_style))
    tally_data = [
        ["Type", "Count"],
        ["Cooperative vessels", str(tally["vessel"])],
        ["Dark vessels (SAR-only)", str(tally["dark_vessel"])],
        ["AIS spoofed", str(tally["ais_spoofed"])],
        ["Port-skipping", str(tally["port_skipping"])],
        ["Loitering", str(tally["loitering_vessel"])],
        ["AIS dropouts (gaps)", str(tally["ais_gap"])],
        ["Convoys in formation", str(len(convoy_ids))],
    ]
    tally_table = Table(tally_data, colWidths=[3.5 * inch, 1 * inch])
    tally_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [rl_colors.white, rl_colors.HexColor("#f5f5f7")]),
    ]))
    story.append(tally_table)

    # Top dark vessels
    if dark_vessels:
        story.append(Paragraph("Top dark vessels (by priority)", h2_style))
        rows = [["Entity", "Lat", "Lon", "Last seen", "Priority"]]
        for ent in dark_vessels:
            rows.append([
                ent.entity_id[:14],
                f"{ent.geom.lat:.4f}" if ent.geom else "—",
                f"{ent.geom.lon:.4f}" if ent.geom else "—",
                ent.last_seen.strftime("%Y-%m-%d %H:%MZ"),
                f"{ent.priority_score:.2f}",
            ])
        dv_table = Table(rows, colWidths=[1.3 * inch, 0.8 * inch,
                                          0.8 * inch, 1.3 * inch, 0.8 * inch])
        dv_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (4, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ]))
        story.append(dv_table)
    else:
        story.append(Paragraph("Top dark vessels: none in current state.", body_style))

    # Dispatch log
    story.append(Paragraph(
        f"Dispatch log ({len(dispatches)} in last 24 h)", h2_style))
    if dispatches:
        rows = [["Seq", "Time (UTC)", "Operator", "Action", "Entity", "Hash"]]
        for d in dispatches[:25]:    # cap at 25 rows to fit one page
            payload = d.payload or {}
            rows.append([
                str(d.seq),
                d.t.strftime("%H:%M"),
                d.actor[:16],
                (payload.get("action_type") or "—")[:16],
                (payload.get("entity_name")
                 or payload.get("entity_id", "")[:12])[:16],
                d.self_hash[:10] + "…",
            ])
        disp_table = Table(rows, colWidths=[0.5 * inch, 0.7 * inch,
                                            1.1 * inch, 1.2 * inch,
                                            1.4 * inch, 0.9 * inch])
        disp_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (0, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
            ("FONTNAME", (5, 1), (5, -1), "Courier"),
        ]))
        story.append(disp_table)
    else:
        story.append(Paragraph(
            "No dispatches filed in the last 24 hours.", body_style))

    # Footer
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        f"Generated by semper-safe at {now.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
        f"Every dispatch above is hash-chained to the audit log; verify "
        f"by walking from audit chain head {audit_head[:20]}… back to "
        f"genesis using POST /audit/verify.",
        meta_style,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    filename = f"semper-safe-brief-{now.strftime('%Y%m%d-%H%M')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/maritime/sanctions")
def maritime_sanctions():
    """Full sanctioned-vessel catalog — MMSI/IMO + flag + program.

    Returned as one bulk response so the frontend can build an
    indexed lookup on entities-tick without N round trips. Cache
    header set since the catalog only changes via a code deploy
    (or, when Phase 5 lands, via the scheduled SDN-feed pull task).
    """
    import sanctions
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"vessels": sanctions.list_sanctioned()},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/maritime/entities/{eid}/registry")
def maritime_entity_registry(eid: str):
    """Public-registry enrichment for an entity (MMSI → flag state, etc).

    Free public sources: the MMSI's first 3 digits (MID) tell us the
    flag state per ITU-R M.585. Paid APIs (Equasis, MarineTraffic)
    fill IMO/owner/class-society fields; without a key we return
    the MID-derived info plus structured placeholders so the frontend
    can show 'pending — Equasis key not configured' for those slots.
    """
    import mmsi_mid
    import sanctions as _sanctions

    ent = maritime.entities.get(eid)
    if ent is None:
        raise HTTPException(404, "entity not found")
    mmsi = (ent.attrs or {}).get("mmsi")
    flag = mmsi_mid.mid_country(mmsi)
    kind = mmsi_mid.mmsi_kind(mmsi)
    sanction = _sanctions.is_sanctioned(mmsi, (ent.attrs or {}).get("imo"))
    return {
        "entity_id": eid,
        "mmsi": mmsi,
        "mmsi_kind": kind,
        "flag": flag,                           # {"country": ..., "iso2": ...} or null
        "vessel_name": (ent.attrs or {}).get("name"),
        "ais_ship_type": (ent.attrs or {}).get("ship_type"),
        "ais_destination": (ent.attrs or {}).get("destination"),
        # Placeholders — wire to Equasis/MarineTraffic when keys
        # are configured. Field shape is stable so the frontend can
        # render them today and start showing real values the moment
        # they're populated.
        "imo": None,
        "owner": None,
        "manager": None,
        "class_society": None,
        "year_built": None,
        "registry_source": "mmsi_mid_only",
        "registry_note": (
            "Flag state derived from MMSI MID prefix (ITU-R M.585). "
            "Owner / IMO / class society require an Equasis or "
            "MarineTraffic API key — not configured."
        ),
        "sanction": sanction,    # null OR {mmsi, imo, name, flag, source, program, note}
    }


@app.get("/maritime/entities/{eid}/audit")
def maritime_entity_audit(eid: str, limit: int = 100):
    """Audit-chain entries tagged to a specific entity.

    Filters audit_log.all() to entries whose payload references the
    entity_id. Useful for surfacing the complete lifecycle of an
    anomaly — creation, reclassifications, recommendations, decisions,
    dispatches — in one place on the side panel.

    Bounded by `limit` (default 100, most recent first). The audit log
    is hot-archived to R2 once it grows past a few hundred entries, so
    deep history may not be in the live table — but for an entity that
    only just emerged today this covers the full chain.
    """
    entries = audit_log.all()
    matches = []
    for e in entries:
        payload = e.payload or {}
        if payload.get("entity_id") == eid:
            matches.append({
                "seq": e.seq,
                "t": e.t.isoformat(),
                "actor": e.actor,
                "event_type": e.event_type,
                "payload": payload,
                "self_hash": e.self_hash,
                "prev_hash": e.prev_hash,
            })
    matches.reverse()
    return {
        "entity_id": eid,
        "n_total_matches": len(matches),
        "entries": matches[:limit],
    }


@app.get("/maritime/onshore_assets")
def maritime_onshore_assets():
    """Return the static onshore-asset catalog (USCG stations, Navy
    facilities) as GeoJSON for the frontend overlay layer.

    Static data — assets don't move at human-decision time scales.
    Cache header lets the browser hold it for an hour.
    """
    from fastapi.responses import JSONResponse
    import onshore_assets

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]},
            "properties": {
                "name": a["name"],
                "type": a["type"],
                "note": a.get("note", ""),
            },
        }
        for a in onshore_assets.list_assets()
    ]
    return JSONResponse(
        {"type": "FeatureCollection", "features": features},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/maritime/dispatches/recent")
def maritime_recent_dispatches(hours: int = 24):
    """Return positions of dispatch_filed audit entries in the last
    `hours` window. Used by the frontend operator-workload heatmap
    to surface 'where the watch has been busy'.

    The dispatch payload was built by maritime_file_dispatch and
    stamps the entity_id, entity_type, entity_name + entity_mmsi
    onto the audit entry. We resolve entity_id → current position
    via the in-memory engine; entities that have been retention-
    swept since the dispatch resolve to None and are omitted.
    """
    from datetime import timedelta as _td

    cutoff = datetime.now(timezone.utc) - _td(hours=hours)
    entries = audit_log.all()
    out = []
    for e in entries:
        if e.event_type != "dispatch_filed":
            continue
        if e.t < cutoff:
            continue
        payload = e.payload or {}
        eid = payload.get("entity_id")
        if not eid:
            continue
        ent = maritime.entities.get(eid)
        if ent is None or ent.geom is None:
            continue
        out.append({
            "audit_seq": e.seq,
            "audit_hash": e.self_hash,
            "dispatched_at": e.t.isoformat(),
            "operator": e.actor,
            "entity_id": eid,
            "entity_type": payload.get("entity_type"),
            "action_type": payload.get("action_type"),
            "lon": ent.geom.lon,
            "lat": ent.geom.lat,
        })
    return {
        "since": cutoff.isoformat(),
        "now": datetime.now(timezone.utc).isoformat(),
        "n_dispatches": len(out),
        "dispatches": out,
    }


@app.post("/maritime/dispatches", status_code=201)
def maritime_file_dispatch(body: DispatchRequest):
    """File a formal dispatch — appends a 'dispatch_filed' audit entry
    and returns the resulting audit hash as a verifiable proof token.

    Why separate from approve/reject:
      - approve/reject act on an existing engine recommendation; they
        require a PENDING rec to exist for the entity.
      - dispatch is the operator's own act-of-record. It works whether
        or not the engine has a recommendation pending, captures a
        free-form notes field, and is intentionally the SAME audit-
        chain mechanism the rest of the system uses (no separate
        dispatches table), so the integrity story is one chain to
        verify, not two.

    Returns: { audit_seq, audit_hash, prev_hash, dispatched_at }
    Frontend renders the hash inline next to the FILE DISPATCH button
    so the operator sees their action's proof immediately.
    """
    ent = maritime.entities.get(body.entity_id)
    if ent is None:
        raise HTTPException(404, "entity not found")

    entry = audit_log.append(
        actor=body.operator,
        event_type="dispatch_filed",
        payload={
            "entity_id": body.entity_id,
            "entity_type": ent.type.value,
            "action_type": body.action_type,
            "notes": body.notes or "",
            "entity_name": ent.attrs.get("name"),
            "entity_mmsi": ent.attrs.get("mmsi"),
        },
    )
    return {
        "audit_seq": entry.seq,
        "audit_hash": entry.self_hash,
        "prev_hash": entry.prev_hash,
        "dispatched_at": entry.t.isoformat(),
        "operator": body.operator,
        "entity_id": body.entity_id,
        "action_type": body.action_type,
    }


# Wildfire domain
@app.get("/wildfire/incidents")
def wildfire_incidents():
    """Active US wildfire incidents from NIFC WFIGS, cached.

    GeoJSON FeatureCollection of incident POO (point of origin)
    locations with name, discovery time, current size (acres),
    containment %, agency, state, behavior, and cause. Updated
    every 5 minutes by the wildfire refresh loop.
    """
    features = []
    for i in _wildfire_cache["incidents"]:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [i["lon"], i["lat"]]},
            "properties": {
                "incident_id":  i["incident_id"],
                "name":         i["name"],
                "discovered_at": i["discovered_at"],
                "size_acres":   i["size_acres"],
                "contained_pct": i["contained_pct"],
                "agency":       i["agency"],
                "state":        i["state"],
                "behavior":     i["behavior"],
                "cause":        i["cause"],
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "n_incidents": len(features),
        "last_refresh_at": _wildfire_cache["last_refresh_at"],
    }


@app.get("/wildfire/dryness")
def wildfire_dryness():
    """Per-cell fuel-dryness index (synthesized NDVI proxy).

    Combines drought tier + HDW + vegetation-class weight into a 0..1
    score. Frontend renders as a brown-yellow heatmap on the wildfire
    tab. Replaceable with NASA MODIS NDVI when the parsing pipeline
    is wired."""
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
            "properties": {
                "dryness":      c["dryness"],
                "drought_tier": c["drought_tier"],
                "veg_class":    c["veg_class"],
                "veg_weight":   c["veg_weight"],
            },
        }
        for c in _wildfire_cache["dryness"]
    ]
    return {"type": "FeatureCollection", "features": features,
            "n_cells": len(features),
            "last_refresh_at": _wildfire_cache["last_refresh_at"]}


@app.get("/wildfire/mutual_aid")
def wildfire_mutual_aid(hours: int = 24):
    """Recent mutual_aid_required audit events.

    Each event is a PSPS de-energization that has triggered an
    operating-agreement notification to neighboring utilities (e.g.
    PG&E PSPS in the North Bay notifies SMUD + TID). The frontend
    surfaces these as toasts on the wildfire tab when they're fresh.
    """
    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(hours=hours)
    out = []
    for entry in audit_log.all():
        if entry.event_type != "mutual_aid_required":
            continue
        if entry.t < cutoff:
            continue
        out.append({
            "audit_seq":  entry.seq,
            "audit_hash": entry.self_hash,
            "t":          entry.t.isoformat(),
            "utility":    (entry.payload or {}).get("utility"),
            "zone_id":    (entry.payload or {}).get("zone_id"),
            "zone_name":  (entry.payload or {}).get("zone_name"),
            "notified":   (entry.payload or {}).get("notified") or [],
            "customers_affected":
                (entry.payload or {}).get("customers_affected"),
            "reason":     (entry.payload or {}).get("reason"),
        })
    out.sort(key=lambda e: -e["audit_seq"])
    return {"events": out, "n_events": len(out),
            "window_hours": hours}


@app.get("/wildfire/psps")
def wildfire_psps():
    """Active / scheduled / standby PSPS shutoff zones from CA IOUs.

    Polygon GeoJSON with utility, status, start/end, customers
    affected, and the NWS-driven reason. Frontend renders as red
    cross-hatched fill; toast when a zone is newly active."""
    features = []
    for z in _wildfire_cache["psps"]:
        geom = z.get("geometry")
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zone_id":  z["zone_id"],
                "utility":  z["utility"],
                "name":     z["name"],
                "status":   z["status"],
                "starts_at": z["starts_at"],
                "ends_at":  z["ends_at"],
                "customers_affected": z["customers_affected"],
                "reason":   z["reason"],
            },
        })
    return {"type": "FeatureCollection", "features": features,
            "n_zones": len(features),
            "last_refresh_at": _wildfire_cache["last_refresh_at"]}


@app.get("/wildfire/wui")
def wildfire_wui():
    """Scored WUI communities — top of the list = highest exposure
    given today's HDW conditions. Frontend renders as points colored
    by tier (extreme / high / elevated / normal) sized by population."""
    features = []
    for c in _wildfire_cache["wui"]:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [c["lon"], c["lat"]]},
            "properties": {
                "name":       c["name"],
                "state":      c["state"],
                "county":     c["county"],
                "pop":        c["pop"],
                "structures": c["structures"],
                "history":    c["history"],
                "score":      c["score"],
                "max_nearby_hdw_risk": c["max_nearby_hdw_risk"],
                "tier":       c["tier"],
            },
        })
    return {"type": "FeatureCollection", "features": features,
            "n_communities": len(features),
            "last_refresh_at": _wildfire_cache["last_refresh_at"]}


@app.get("/wildfire/smoke")
def wildfire_smoke():
    """Modeled smoke plume polygons projected downwind from active
    NIFC incidents. Wedge geometry sized by incident acres × wind
    speed. Replaceable with NOAA HRRR-Smoke when wired."""
    features = []
    for p in _wildfire_cache["smoke"]:
        features.append({
            "type": "Feature",
            "geometry": p["geometry"],
            "properties": {
                "incident_id":      p["incident_id"],
                "incident_name":    p["incident_name"],
                "bearing_from_deg": p["bearing_from_deg"],
                "wind_speed_ms":    p["wind_speed_ms"],
                "length_km":        p["length_km"],
                "size_acres":       p["size_acres"],
            },
        })
    return {"type": "FeatureCollection", "features": features,
            "n_plumes": len(features),
            "last_refresh_at": _wildfire_cache["last_refresh_at"]}


@app.get("/wildfire/history")
def wildfire_history():
    """3-year NIFC interagency historical perimeters (≥1000 acres)
    in the Western US AOI. Frontend renders as a faded brown polygon
    fill so the operator sees which areas have NOT burned recently
    (highest fuel load)."""
    features = []
    for h in _wildfire_cache["history"]:
        features.append({
            "type": "Feature",
            "geometry": h["geometry"],
            "properties": {
                "fire_name": h["fire_name"],
                "fire_year": h["fire_year"],
                "acres":     h["acres"],
                "agency":    h["agency"],
            },
        })
    return {"type": "FeatureCollection", "features": features,
            "n_perimeters": len(features),
            "last_refresh_at": _wildfire_cache["history_last_refresh"].isoformat()
                if _wildfire_cache["history_last_refresh"] else None}


@app.get("/wildfire/wui/{community}/draft_alert")
def wildfire_draft_alert(community: str):
    """Generate a DRAFT IPAWS-style Wireless Emergency Alert for a
    high-risk WUI community.

    Returns a structured alert payload (text + 5-mile circular polygon)
    ready for an authorizing officer to review. THE SYSTEM DOES NOT
    SEND THESE — actual IPAWS publication requires a signed FEMA
    AlertCertificate the operator holds out-of-band.

    Designed for the demo workflow: an operator looking at an
    extreme-tier community can click 'draft alert', review the
    auto-generated wording for typos / over-broad scope, then send
    it through their existing IPAWS interface."""
    import math

    target = None
    for c in _wildfire_cache.get("wui") or []:
        if c.get("name", "").lower() == community.lower():
            target = c
            break
    if target is None:
        raise HTTPException(404, f"WUI community '{community}' not found")

    name = target["name"]
    state = target["state"]
    tier = target.get("tier", "elevated")
    pop = target.get("pop", "?")
    hdw = target.get("max_nearby_hdw_risk", 0)

    # Severity-tier wording. Real IPAWS WEA messages are capped at
    # 360 chars (Long-Form Alert); we stay under that.
    if tier == "extreme":
        action = "EVACUATE NOW"
        body = (
            f"WILDFIRE EVACUATION ORDER: Extreme fire-weather risk in "
            f"{name}, {state}. Leave area immediately along designated "
            f"egress routes. Take 6 P's: People, Pets, Papers, Phone, "
            f"Plastic, Prescriptions. Listen for instructions."
        )
    elif tier == "high":
        action = "PREPARE TO EVACUATE"
        body = (
            f"WILDFIRE EVACUATION WARNING: Elevated fire-weather risk "
            f"in {name}, {state}. Prepare to leave on short notice. "
            f"Pack the 6 P's; identify two evacuation routes; monitor "
            f"local alerts."
        )
    else:
        action = "STAY ALERT"
        body = (
            f"FIRE-WEATHER ADVISORY: Above-normal risk in {name}, "
            f"{state}. No evacuation order. Avoid outdoor flame; "
            f"clear defensible space; monitor conditions."
        )

    headline = (
        f"[DRAFT WEA] {action} — {name}, {state} "
        f"(HDW risk {hdw:.2f}, pop ≈ {pop}k)"
    )

    # 5-mile (~8 km) alert polygon centered on the community.
    lon0, lat0 = target["lon"], target["lat"]
    radius_km = 8.0
    R = 6371.0
    poly = []
    for i in range(33):
        bearing = math.radians(i * (360.0 / 32))
        d = radius_km / R
        lat = math.asin(
            math.sin(math.radians(lat0)) * math.cos(d)
            + math.cos(math.radians(lat0)) * math.sin(d) * math.cos(bearing)
        )
        lon = math.radians(lon0) + math.atan2(
            math.sin(bearing) * math.sin(d) * math.cos(math.radians(lat0)),
            math.cos(d) - math.sin(math.radians(lat0)) * math.sin(lat),
        )
        poly.append([math.degrees(lon), math.degrees(lat)])

    from datetime import timedelta as _td
    return {
        "draft": True,
        "alert_kind": "IPAWS_WEA_DRAFT",
        "headline": headline,
        "action": action,
        "body": body,
        "tier": tier,
        "community": name,
        "state": state,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + _td(hours=6)).isoformat(),
        "alert_polygon": {
            "type": "Polygon",
            "coordinates": [poly],
        },
        "delivery_note": (
            "DRAFT. This is a system-generated proposal. Operator "
            "must review wording, scope, and authority before sending "
            "via IPAWS / Cal OES / state EAS. NOT broadcast by this "
            "system."
        ),
    }


@app.post("/wildfire/dispatches", status_code=201)
def wildfire_file_dispatch(body: DispatchRequest):
    """Operator files a fire-side dispatch — parallel to maritime.

    Target entity_id can be a wildfire entity (hotspot / fire_event /
    lightning_ignition_risk / fire_preposition) OR a preposition
    recommendation id ('pre_*') OR a WUI community id, depending on
    what the operator clicked. We resolve via the wildfire engine
    first, then fall back to the preposition cache for pre_* IDs.

    Same audit-chain mechanism as maritime: appends a 'dispatch_filed'
    entry and returns the resulting audit hash."""
    eid = body.entity_id
    target_type = None
    target_name = None

    ent = wildfire.entities.get(eid)
    if ent is not None:
        target_type = ent.type.value
        target_name = (ent.attrs or {}).get("name")
    else:
        # Maybe a preposition recommendation id
        for rec in _wildfire_cache.get("preposition") or []:
            if rec.get("id") == eid:
                target_type = "fire_preposition"
                target_name = (rec.get("nearest_asset") or {}).get("name")
                break
    if target_type is None:
        # Allow free-form wildfire targets (WUI community names, etc) —
        # the operator's notes field documents intent.
        target_type = "wildfire_freeform"

    entry = audit_log.append(
        actor=body.operator,
        event_type="dispatch_filed",
        payload={
            "domain":      "wildfire",
            "entity_id":   eid,
            "entity_type": target_type,
            "action_type": body.action_type,
            "notes":       body.notes or "",
            "entity_name": target_name,
        },
    )
    return {
        "audit_seq":      entry.seq,
        "audit_hash":     entry.self_hash,
        "prev_hash":      entry.prev_hash,
        "dispatched_at":  entry.t.isoformat(),
        "operator":       body.operator,
        "entity_id":      eid,
        "action_type":    body.action_type,
    }


@app.get("/wildfire/daily_brief.pdf")
def wildfire_daily_brief():
    """One-page wildfire-ops PDF brief.

    Header with audit chain head + window timestamp; tally of active
    NIFC incidents; top WUI communities by tier; active PSPS zones;
    active Red Flag warnings; recent preposition recommendations.
    Designed as the watch-officer leave-behind."""
    from io import BytesIO
    from fastapi.responses import Response
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except ImportError as exc:
        raise HTTPException(
            501,
            f"PDF brief requires reportlab — pip install reportlab ({exc})",
        )

    now = datetime.now(timezone.utc)
    incidents = _wildfire_cache.get("incidents") or []
    rfw       = _wildfire_cache.get("red_flag") or []
    psps      = _wildfire_cache.get("psps") or []
    wui       = _wildfire_cache.get("wui") or []
    pre       = _wildfire_cache.get("preposition") or []
    history   = _wildfire_cache.get("history") or []
    head = audit_log.head()
    audit_count = audit_log.count() if hasattr(audit_log, "count") else len(audit_log.all())

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "wfTitle", parent=styles["Title"],
        fontSize=16, textColor=rl_colors.HexColor("#0a1118"), spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "wfMeta", parent=styles["Normal"], fontSize=8,
        textColor=rl_colors.HexColor("#666"), spaceAfter=10, leading=10,
    )
    h2_style = ParagraphStyle(
        "wfH2", parent=styles["Heading2"], fontSize=11,
        textColor=rl_colors.HexColor("#0a1118"), spaceBefore=10, spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "wfBody", parent=styles["Normal"], fontSize=8,
        textColor=rl_colors.HexColor("#1a2330"), leading=10,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title="Semper Safe — Wildfire Daily Brief",
    )
    story = []
    story.append(Paragraph("Semper Safe · Wildfire Daily Brief", title_style))
    story.append(Paragraph(
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} &nbsp;·&nbsp; "
        f"Audit chain head: {head[:24]}… &nbsp;·&nbsp; "
        f"{audit_count} audit entries", meta_style,
    ))

    # Active incidents tally
    story.append(Paragraph(
        f"Active wildfire incidents · NIFC ({len(incidents)} total)",
        h2_style))
    if incidents:
        # Top 10 by size
        top = sorted(
            [i for i in incidents if i.get("size_acres")],
            key=lambda i: -float(i.get("size_acres") or 0),
        )[:10]
        rows = [["Name", "State", "Acres", "Contained %", "Behavior"]]
        for i in top:
            rows.append([
                (i.get("name") or "")[:32],
                i.get("state") or "—",
                f"{int(i.get('size_acres') or 0):,}",
                f"{i.get('contained_pct') or 0}%",
                (i.get("behavior") or "—")[:18],
            ])
        t = Table(rows, colWidths=[2.0 * inch, 0.55 * inch,
                                    0.7 * inch, 0.85 * inch, 1.2 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (3, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No active incidents in the AOI.", body_style))

    # WUI tier rollup
    extreme = [c for c in wui if c.get("tier") == "extreme"]
    high    = [c for c in wui if c.get("tier") == "high"]
    elev    = [c for c in wui if c.get("tier") == "elevated"]
    story.append(Paragraph(
        f"WUI exposure · {len(extreme)} extreme · {len(high)} high · "
        f"{len(elev)} elevated", h2_style))
    if extreme or high:
        rows = [["Community", "Pop", "Tier", "Today's HDW"]]
        for c in (extreme + high)[:10]:
            rows.append([
                c.get("name") or "—",
                f"{c.get('pop', '?')}k",
                (c.get("tier") or "—").upper(),
                f"{c.get('max_nearby_hdw_risk', 0):.2f}",
            ])
        t = Table(rows, colWidths=[2.0 * inch, 0.6 * inch,
                                    0.9 * inch, 1.0 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (3, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No WUI communities currently in elevated tier or above.", body_style))

    # PSPS
    story.append(Paragraph(f"PSPS zones · {len(psps)} catalog entries", h2_style))
    if psps:
        rows = [["Utility", "Zone", "Status", "Customers"]]
        for z in psps:
            rows.append([
                z.get("utility") or "—",
                (z.get("name") or "")[:30],
                (z.get("status") or "—").upper(),
                f"{int(z.get('customers_affected') or 0):,}",
            ])
        t = Table(rows, colWidths=[0.8 * inch, 2.5 * inch,
                                    0.9 * inch, 0.9 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ]))
        story.append(t)

    # Pre-position recommendations
    story.append(Paragraph(
        f"Pre-position recommendations · {len(pre)} active", h2_style))
    if pre:
        rows = [["Score", "Asset (nearest)", "Distance", "HDW"]]
        for p in pre:
            a = p.get("nearest_asset") or {}
            rows.append([
                f"{p.get('score', 0):.2f}",
                (a.get("name") or "—")[:30],
                f"{a.get('distance_km', 0):.0f} km",
                f"{p.get('hdw', 0):.0f}",
            ])
        t = Table(rows, colWidths=[0.7 * inch, 2.5 * inch,
                                    0.9 * inch, 0.7 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a2330")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 0.25, rl_colors.HexColor("#cccccc")),
        ]))
        story.append(t)

    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        f"Historical perimeters cached: {len(history)} fires ≥1000 acres "
        f"in the AOI over the last 3 years.",
        body_style))
    story.append(Paragraph(
        f"Verify any dispatch in this brief by walking the audit chain "
        f"back from head {head[:20]}… (POST /audit/verify).",
        meta_style,
    ))
    doc.build(story)
    pdf_bytes = buf.getvalue()
    filename = f"semper-safe-wildfire-brief-{now.strftime('%Y%m%d-%H%M')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/wildfire/assets")
def wildfire_assets():
    """Fire-asset catalog — CAL FIRE Air Attack Bases, USFS air-attack,
    helitack, IABs, regional ops centers. Static data; cached by the
    browser for an hour."""
    from fastapi.responses import JSONResponse
    import wildfire_preposition
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [a["lon"], a["lat"]]},
            "properties": {"name": a["name"], "type": a["type"]},
        }
        for a in wildfire_preposition.list_assets()
    ]
    return JSONResponse(
        {"type": "FeatureCollection", "features": features},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/wildfire/preposition")
def wildfire_preposition_endpoint():
    """Engine-recommended preposition stagings for the next 24 h.

    Computed from the cached ignition-risk grid and active NIFC
    incidents on each wildfire refresh tick. Each feature is a Point
    at the suggested staging coordinate with the nearest fire asset,
    coverage gap, and a one-paragraph rationale string."""
    features = []
    for p in _wildfire_cache["preposition"]:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [p["lon"], p["lat"]]},
            "properties": {
                "id":            p["id"],
                "score":         p["score"],
                "risk_score":    p["risk_score"],
                "hdw":           p["hdw"],
                "nearest_asset": p["nearest_asset"],
                "rationale":     p["rationale"],
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "n_recommendations": len(features),
        "last_refresh_at": _wildfire_cache["last_refresh_at"],
    }


@app.get("/wildfire/lightning")
def wildfire_lightning():
    """Last-hour cloud-to-ground lightning strikes inside the AOI.

    Synthesized from NWS Severe Thunderstorm Warning polygons (a
    free public proxy for real lightning), with a per-strike polarity,
    amplitude, and timestamp. Frontend renders as glowing yellow
    points with a 1-hour fade. Real Vaisala / Earth Networks feeds
    would slot in via the same dict shape.
    """
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            "properties": {
                "id":           s["id"],
                "t":            s["t"],
                "polarity":     s["polarity"],
                "amplitude_ka": s["amplitude_ka"],
                "source":       s["source"],
            },
        }
        for s in _wildfire_cache["lightning"]
    ]
    return {
        "type": "FeatureCollection",
        "features": features,
        "n_strikes": len(features),
        "source": "nws_thunderstorm_proxy",
        "last_refresh_at": _wildfire_cache["last_refresh_at"],
    }


@app.get("/wildfire/risk_grid")
def wildfire_risk_grid():
    """Ignition-risk grid — Hot-Dry-Windy index per AOI cell.

    Each feature is a Point at the cell center with the HDW value,
    normalized risk_score [0,1], and the peak forecast conditions
    (temp / RH / wind) that produced the score. Frontend renders
    as a colored heatmap; clicking a cell pops the conditions.
    """
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
            "properties": {
                "hdw":          c["hdw"],
                "risk_score":   c["risk_score"],
                "peak_temp_c":  c["peak_temp_c"],
                "peak_rh_pct":  c["peak_rh_pct"],
                "peak_wind_ms": c["peak_wind_ms"],
            },
        }
        for c in _wildfire_cache["risk_grid"]
    ]
    return {
        "type": "FeatureCollection",
        "features": features,
        "n_cells": len(features),
        "last_refresh_at": _wildfire_cache["last_refresh_at"],
    }


@app.get("/wildfire/red_flag")
def wildfire_red_flag():
    """Active NWS Red Flag Warnings + Fire Weather Watches as GeoJSON.

    Cached output from the wildfire refresh loop — polygons + headline
    + severity per active alert. The frontend renders these as a
    translucent red fill layer so the operator sees the next 12-24 h
    of high-risk geographies at a glance.
    """
    features = []
    for a in _wildfire_cache["red_flag"]:
        geom = a.get("geometry")
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id":        a.get("id"),
                "event":     a.get("event"),
                "severity":  a.get("severity"),
                "urgency":   a.get("urgency"),
                "certainty": a.get("certainty"),
                "effective": a.get("effective"),
                "expires":   a.get("expires"),
                "ends":      a.get("ends"),
                "headline":  a.get("headline"),
                "sender":    a.get("sender"),
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "n_alerts": len(features),
        "last_refresh_at": _wildfire_cache["last_refresh_at"],
    }


@app.get("/wildfire/incidents/{incident_id}/perimeter_history")
def wildfire_incident_perimeter_history(incident_id: str):
    """Per-incident size + containment snapshots over the last 8 hours.

    Backed by a per-tick capture inside the wildfire refresh loop —
    each entry is {t, size_acres, contained_pct}. Frontend popup
    renders a tiny sparkline so the operator sees growth velocity at
    a glance without leaving the map."""
    series = _wildfire_cache["perimeter_snapshots"].get(incident_id, [])
    return {
        "incident_id":   incident_id,
        "n_samples":     len(series),
        "snapshots":     series,
    }


@app.get("/wildfire/incidents/{incident_id}/perimeter")
def wildfire_incident_perimeter(incident_id: str):
    """Polygon perimeter for one incident from NIFC, when mapped.

    Many small fires lack a mapped perimeter; returns 404 in that
    case so the frontend can fall back to the POO point.
    """
    import nifc
    p = nifc.fetch_incident_perimeter(incident_id)
    if p is None:
        raise HTTPException(404, "no perimeter mapped for this incident")
    return p


@app.get("/wildfire/entities")
def wildfire_entities():
    rows = sorted(list(wildfire.entities.values()),
                  key=lambda e: (-e.priority_score, -e.last_seen.timestamp()))
    return {"entities": [e.model_dump() for e in rows]}


@app.get("/wildfire/entities/{eid}/track")
def wildfire_track(eid: str, limit: int = 200):
    return _entity_track(wildfire, eid, limit)


@app.get("/wildfire/timeline")
def wildfire_timeline(at: str | None = None, lookback_minutes: int = 60):
    return _timeline("wildfire", at, lookback_minutes)


# --- Admin endpoints for manual SAR pipeline triggers ----------------
#
# Synchronous and BLOCKING — fine for one-off operator triggers, NOT for
# general traffic. Gated by an ADMIN_TOKEN header so they're not
# browseable. Set ADMIN_TOKEN as a Fly secret.

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _require_admin(x_admin_token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "missing or invalid X-Admin-Token")


@app.get("/maritime/realtime")
def maritime_realtime():
    """Single-call freshness summary for every sensor in the platform.

    Operator-grade "system pulse" — the panel that goes in the corner of
    the map and lets you tell at a glance whether each layer is alive
    and current. Each sensor returns:
      latest_t          ISO-8601 of the most recent observation/event
      age_seconds       now - latest_t, for client-side rendering
      detail            small free-form blob with sensor-specific extras

    Designed to be cheap — pure DB aggregates + a couple of in-memory
    reads. Polled every 30 s on the frontend.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select as sa_select, func
    from db import models as dbm
    from db.session import session_scope

    now = datetime.now(timezone.utc)

    def _delta(t):
        if t is None:
            return None, None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.isoformat(), int((now - t).total_seconds())

    out = {"now": now.isoformat()}

    if not store.is_persistent():
        out["persistent"] = False
        return out

    with session_scope() as s:
        # AIS: most recent entity update (vessels + ais_gap entities)
        ais_t = s.execute(
            sa_select(func.max(dbm.EntityRow.last_seen))
            .where(dbm.EntityRow.domain == "maritime")
            .where(dbm.EntityRow.type.in_(["vessel", "ais_gap"]))
        ).scalar_one()
        ais_iso, ais_age = _delta(ais_t)
        n_vessels = s.execute(
            sa_select(func.count())
            .select_from(dbm.EntityRow)
            .where(dbm.EntityRow.domain == "maritime")
            .where(dbm.EntityRow.type == "vessel")
        ).scalar_one()
        out["ais"] = {
            "latest_t": ais_iso,
            "age_seconds": ais_age,
            "detail": {"n_vessels": int(n_vessels or 0)},
        }

        # SAR: latest detection
        sar_det_t, n_det, n_dark = s.execute(
            sa_select(
                func.max(dbm.SarDetectionRow.detected_at),
                func.count(),
                func.count().filter(dbm.SarDetectionRow.matched_entity_id.is_(None)),
            )
        ).one()
        # SAR scene state histogram
        scene_state_rows = s.execute(
            sa_select(dbm.SarSceneRow.state, func.count())
            .group_by(dbm.SarSceneRow.state)
        ).all()
        sar_iso, sar_age = _delta(sar_det_t)
        out["sar"] = {
            "latest_t": sar_iso,
            "age_seconds": sar_age,
            "detail": {
                "n_detections": int(n_det or 0),
                "n_dark": int(n_dark or 0),
                "scene_states": {st: int(c) for st, c in scene_state_rows},
            },
        }

        # S2: latest catalogued scene (any state)
        s2_t = s.execute(
            sa_select(func.max(dbm.S2SceneRow.acquired_at))
        ).scalar_one()
        s2_states = {
            st: int(c) for st, c in s.execute(
                sa_select(dbm.S2SceneRow.state, func.count())
                .group_by(dbm.S2SceneRow.state)
            ).all()
        }
        s2_iso, s2_age = _delta(s2_t)
        out["s2"] = {
            "latest_t": s2_iso,
            "age_seconds": s2_age,
            "detail": {"scene_states": s2_states},
        }

    # Buoys — single import, network-bound. Skip if anything goes wrong;
    # the realtime endpoint shouldn't 500 because NDBC is unhappy.
    try:
        import ndbc
        rows = ndbc.fetch_all()
        latest_t = None
        n_alive = 0
        for r in rows:
            obs = r.get("observation")
            if obs and obs.get("t"):
                t = datetime.fromisoformat(obs["t"])
                if latest_t is None or t > latest_t:
                    latest_t = t
                n_alive += 1
        b_iso, b_age = _delta(latest_t)
        out["buoys"] = {
            "latest_t": b_iso,
            "age_seconds": b_age,
            "detail": {"n_total": len(rows), "n_alive": n_alive},
        }
    except Exception as exc:  # noqa: BLE001
        out["buoys"] = {"error": str(exc)}

    # Audit chain — head + count
    out["audit"] = {
        "head": audit_log.head(),
        "entries": len(audit_log.all()),
    }

    return out


@app.get("/maritime/buoys")
def maritime_buoys():
    """Latest observation from each NDBC buoy in / near the Texas AOI.

    Real-time-most-of-the-time: NDBC publishes new readings every
    ~30 minutes per station. Returns a GeoJSON FeatureCollection so
    the frontend can drop it straight into a MapLibre source.

    Buoys with no observation (stations that 404'd or whose latest
    row was unparseable) come back with properties.observation=null
    so the operator sees a "lost telemetry" marker rather than the
    buoy disappearing from the map.
    """
    import ndbc
    rows = ndbc.fetch_all()
    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "station_id": r["station_id"],
                "name": r["name"],
                "observation": r["observation"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/maritime/s2/scenes")
def maritime_s2_scenes(
    state: str | None = None,
    since_hours: int = 168,
    max_cloud: float | None = None,
    limit: int = 50,
):
    """Sentinel-2 L2A scene footprints (Polygon GeoJSON) for the AOI.

    Default = last 7 days, all states. max_cloud filters server-side
    when cloud_cover_pct is populated; scenes with NULL cloud_cover are
    always returned (operator can re-filter client-side).
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select as sa_select, or_
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    q = sa_select(dbm.S2SceneRow).where(dbm.S2SceneRow.acquired_at >= cutoff)
    if state:
        q = q.where(dbm.S2SceneRow.state == state)
    if max_cloud is not None:
        q = q.where(or_(
            dbm.S2SceneRow.cloud_cover_pct.is_(None),
            dbm.S2SceneRow.cloud_cover_pct <= max_cloud,
        ))
    q = q.order_by(dbm.S2SceneRow.acquired_at.desc()).limit(limit)

    features = []
    with session_scope() as s:
        rows = s.execute(q).scalars().all()
        for r in rows:
            poly = to_shape(r.footprint)
            attrs = r.attrs or {}
            features.append({
                "type": "Feature",
                "geometry": poly.__geo_interface__,
                "properties": {
                    "scene_id": r.scene_id,
                    "platform": r.platform,
                    "product_type": r.product_type,
                    "acquired_at": r.acquired_at.isoformat(),
                    "state": r.state,
                    "cloud_cover_pct": r.cloud_cover_pct,
                    "name": attrs.get("name"),
                },
            })
    return {"type": "FeatureCollection", "features": features}


@app.post("/admin/s2/discover")
def admin_s2_discover(x_admin_token: str | None = Header(default=None)):
    """Manually trigger a Sentinel-2 catalog discovery sweep."""
    _require_admin(x_admin_token)
    scenes = s2.discover_scenes(limit=50)
    result = s2.record_scenes(scenes)
    audit_log.append(
        actor="admin", event_type="s2_discover_manual",
        payload={"discovered": len(scenes), **result},
    )
    return {"discovered": len(scenes), **result}


@app.get("/admin/s2/scenes")
def admin_s2_scenes(
    state: str | None = None,
    limit: int = 50,
    x_admin_token: str | None = Header(default=None),
):
    """Operational listing of S2 scenes — same shape as /admin/sar/scenes
    but with cloud_cover_pct surfaced."""
    _require_admin(x_admin_token)
    from sqlalchemy import select as sa_select
    from db import models as dbm
    from db.session import session_scope

    q = sa_select(
        dbm.S2SceneRow.scene_id, dbm.S2SceneRow.platform,
        dbm.S2SceneRow.acquired_at, dbm.S2SceneRow.state,
        dbm.S2SceneRow.raw_url, dbm.S2SceneRow.failure_reason,
        dbm.S2SceneRow.cloud_cover_pct, dbm.S2SceneRow.attrs,
    )
    if state:
        q = q.where(dbm.S2SceneRow.state == state)
    q = q.order_by(dbm.S2SceneRow.acquired_at.desc()).limit(limit)
    with session_scope() as s:
        rows = s.execute(q).all()
    out = []
    for r in rows:
        attrs = r.attrs or {}
        out.append({
            "scene_id": r.scene_id,
            "platform": r.platform,
            "acquired_at": r.acquired_at.isoformat() if r.acquired_at else None,
            "state": r.state,
            "raw_url": r.raw_url,
            "failure_reason": r.failure_reason,
            "cloud_cover_pct": r.cloud_cover_pct,
            "content_length_bytes": attrs.get("content_length_bytes"),
            "name": attrs.get("name"),
        })
    return {"count": len(out), "scenes": out}


def _do_ais_backfill(start_iso: str, end_iso: str, ingest: bool) -> None:
    """Background-task body for /admin/ais/backfill — runs the long
    daily-file download + filter without blocking the HTTP request."""
    from datetime import datetime, timezone
    import noaa_ais
    def _parse(s: str) -> datetime:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        time_lo = _parse(start_iso)
        time_hi = _parse(end_iso)
        result = noaa_ais.fetch_window(time_lo, time_hi)
        payload = {
            "window": {"start": time_lo.isoformat(), "end": time_hi.isoformat()},
            "total_kept": result["total_kept"],
            "n_unique_mmsi": result["n_unique_mmsi"],
            "missing_days": result["missing_days"],
        }
        if ingest and result["rows"]:
            ing = noaa_ais.ingest_into_engine(result["rows"], engine=maritime)
            payload["ingest_counts"] = ing
        audit_log.append(
            actor="admin", event_type="noaa_ais_backfill",
            payload=payload,
        )
        log.info("AIS backfill done: %s", payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("AIS backfill failed: %s", exc)


@app.post("/admin/ais/backfill", status_code=202)
def admin_ais_backfill(
    start: str,                      # ISO-8601 start time
    end: str,                        # ISO-8601 end time
    background_tasks: BackgroundTasks,
    ingest: bool = False,            # if true, ingest into the live maritime engine
    x_admin_token: str | None = Header(default=None),
):
    """Backfill historical AIS from NOAA Marine Cadastre into the engine.

    Returns 202 immediately; the actual download (300-500 MB per daily
    file, often 1-3 minutes wall clock) runs as a FastAPI BackgroundTask
    and audit-logs noaa_ais_backfill on completion.

    Use case: a SAR scene acquired more than 24 h ago has no AIS in our
    DB to fuse against. /admin/sar/fuse/{scene_id} alone produces
    all-dark detections. Run this first to populate history, then
    re-run fuse — or use /admin/ais/backfill-for-sar/{scene_id} which
    chains both steps automatically.

    Coverage caveat: NOAA's archive lags real-time by 3-6 months.
    Daily files for very recent dates return 404; missing_days appears
    in the audit_log payload.
    """
    _require_admin(x_admin_token)
    from datetime import datetime, timezone
    def _parse(s: str) -> datetime:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        time_lo = _parse(start)
        time_hi = _parse(end)
    except ValueError as exc:
        raise HTTPException(400, f"bad ISO-8601 timestamp: {exc}")
    if time_hi <= time_lo:
        raise HTTPException(400, "end must be after start")
    span_h = (time_hi - time_lo).total_seconds() / 3600
    if span_h > 24:
        raise HTTPException(400,
            "max 24-hour window per call (each daily file is ~400 MB)")
    background_tasks.add_task(_do_ais_backfill, start, end, ingest)
    return {"status": "queued",
            "window": {"start": time_lo.isoformat(),
                        "end": time_hi.isoformat()},
            "tip": "watch audit log for event_type=noaa_ais_backfill"}


def _do_ais_backfill_for_sar(scene_id: str, window_minutes: int,
                              ingest: bool, rerun_fusion: bool) -> None:
    from datetime import timedelta
    from db import models as dbm
    from db.session import session_scope
    import noaa_ais
    try:
        with session_scope() as s:
            scene = s.get(dbm.SarSceneRow, scene_id)
            if scene is None:
                log.warning("backfill-for-sar: scene not found %s", scene_id)
                return
            sar_t = scene.acquired_at
        time_lo = sar_t - timedelta(minutes=window_minutes)
        time_hi = sar_t + timedelta(minutes=window_minutes)
        result = noaa_ais.fetch_window(time_lo, time_hi)
        payload = {
            "scene_id": scene_id,
            "scene_acquired_at": sar_t.isoformat(),
            "window_minutes": window_minutes,
            "total_kept": result["total_kept"],
            "n_unique_mmsi": result["n_unique_mmsi"],
            "missing_days": result["missing_days"],
        }
        if result["missing_days"]:
            payload["status"] = "noaa_archive_not_yet_available"
        if ingest and result["rows"]:
            ing = noaa_ais.ingest_into_engine(result["rows"], engine=maritime)
            payload["ingest_counts"] = ing
            if rerun_fusion:
                import sar_processor
                payload["fusion"] = sar_processor.fuse_detections(
                    maritime, scene_id)
        audit_log.append(
            actor="admin", event_type="noaa_ais_backfill_for_sar",
            payload=payload,
        )
        log.info("AIS backfill-for-sar done: %s", payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("AIS backfill-for-sar failed: %s", exc)


@app.post("/admin/ais/backfill-for-sar/{scene_id}", status_code=202)
def admin_ais_backfill_for_sar(
    scene_id: str,
    background_tasks: BackgroundTasks,
    window_minutes: int = 30,
    ingest: bool = True,
    rerun_fusion: bool = True,
    x_admin_token: str | None = Header(default=None),
):
    """Returns 202 immediately. BackgroundTask:
       fetch ± window_minutes around the SAR scene's acquired_at →
       ingest if requested → re-fuse on the same scene if requested.

    For our 2026 SAR scenes the NOAA archive isn't published yet, so
    the audit-log entry will include status=noaa_archive_not_yet_available
    rather than match counts. Re-run after NOAA catches up (typically
    3-6 months post-acquisition).
    """
    _require_admin(x_admin_token)
    from sqlalchemy import select as sa_select
    from db import models as dbm
    from db.session import session_scope
    with session_scope() as s:
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is None:
            raise HTTPException(404, f"sar scene not found: {scene_id}")
        scene_t = scene.acquired_at.isoformat()
    background_tasks.add_task(
        _do_ais_backfill_for_sar, scene_id, window_minutes, ingest, rerun_fusion,
    )
    return {"status": "queued", "scene_id": scene_id,
            "scene_acquired_at": scene_t,
            "window_minutes": window_minutes,
            "tip": "watch audit log for event_type=noaa_ais_backfill_for_sar"}


def _do_s2_download(scene_id: str) -> None:
    """Background-task body for streaming an S2 scene to R2."""
    try:
        result = s2.download_scene_to_r2(scene_id)
        audit_log.append(
            actor="admin", event_type="s2_scene_downloaded",
            payload={"scene_id": scene_id,
                     "bytes": result.get("bytes"),
                     "parts": result.get("parts"),
                     "skipped": result.get("skipped")},
        )
        log.info("admin S2 download done: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.exception("admin S2 download failed: %s", exc)


@app.post("/admin/s2/download/{scene_id}", status_code=202)
def admin_s2_download(
    scene_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None),
):
    """Kick off a Sentinel-2 scene multipart download to R2.

    Returns 202; the actual transfer (~5 min for a 1 GB L2A scene) runs
    as a FastAPI BackgroundTask so the AIS / health-check loops keep
    ticking. Watch s2_scenes.state to see 'discovered' → 'downloaded'.
    """
    _require_admin(x_admin_token)
    background_tasks.add_task(_do_s2_download, scene_id)
    return {"scene_id": scene_id, "status": "queued",
            "tip": "poll s2_scenes.state for completion"}


@app.get("/maritime/sar/entities/{entity_id}/track")
def maritime_sar_entity_track(entity_id: str, limit: int = 50):
    """Return the SAR-only track for an entity — every sar_detections
    row linked to this entity (across scenes), ordered by detected_at.

    Used by the frontend to draw a polyline through multi-pass dark
    vessel detections (track continuity within fusion._best_dark_vessel_match's
    12 km / 90 min window). For AIS-matched vessels the existing
    /maritime/entities/{eid}/track endpoint returns the AIS-only track,
    but cross-scene SAR confirmation is its own thing.
    """
    from sqlalchemy import select as sa_select
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        rows = s.execute(
            sa_select(dbm.SarDetectionRow)
            .where(dbm.SarDetectionRow.entity_id == entity_id)
            .order_by(dbm.SarDetectionRow.detected_at.asc())
            .limit(limit)
        ).scalars().all()
        out = []
        for r in rows:
            pt = to_shape(r.geom)
            out.append({
                "detection_id": r.detection_id,
                "scene_id": r.scene_id,
                "detected_at": r.detected_at.isoformat(),
                "lat": pt.y, "lon": pt.x,
                "rcs_db": r.rcs_db, "length_m": r.length_m,
                "vv_vh_ratio_db": r.vv_vh_ratio_db,
            })
    return {"entity_id": entity_id, "track": out, "n": len(out)}


@app.get("/maritime/sar/detections/{detection_id}/optical_chip")
def maritime_sar_optical_chip(
    detection_id: str,
    half_size_m: float | None = None,
):
    """Serve a Sentinel-2 RGB chip centered on the SAR detection.

    Returns image/jpeg on success. Returns 404 if the detection or
    matching S2 scene doesn't exist; 202 (with body) if the matching
    S2 scene hasn't been downloaded yet — caller should re-poll after
    triggering /admin/s2/download/{scene_id}.

    First request for a given detection generates the chip and caches
    it in R2; subsequent requests are served from cache (~ms).
    """
    from fastapi.responses import Response, JSONResponse
    from sqlalchemy import select as sa_select
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        det = s.execute(
            sa_select(dbm.SarDetectionRow)
            .where(dbm.SarDetectionRow.detection_id == detection_id)
        ).scalar_one_or_none()
        if det is None:
            raise HTTPException(404, f"detection not found: {detection_id}")
        sar_scene_id = det.scene_id
        pt = to_shape(det.geom)
        det_lat, det_lon = pt.y, pt.x
        det_length_m = det.length_m
        # SAR scene's acquired_at is "near" for time matching.
        sar_scene = s.get(dbm.SarSceneRow, sar_scene_id)
        near_t = sar_scene.acquired_at if sar_scene else det.detected_at

    # Match by the DETECTION point — Sentinel-1 GRDH covers ~250 km, so an
    # S2 tile that overlaps the SAR bounds may not contain the actual
    # detection coordinates.
    s2_scene_id = s2.find_nearest_s2_for_point(
        det_lat, det_lon, near_t,
        max_days=3, max_cloud_pct=40.0,
    )
    if s2_scene_id is None:
        raise HTTPException(
            404,
            "no Sentinel-2 scene within ±3 days and ≤40% cloud "
            "overlaps this detection's SAR scene",
        )

    # Make sure the matched S2 scene is downloaded.
    with session_scope() as s:
        s2_scene = s.get(dbm.S2SceneRow, s2_scene_id)
        if s2_scene is None or s2_scene.state != "downloaded":
            return JSONResponse(
                status_code=202,
                content={
                    "status": "s2_scene_not_yet_downloaded",
                    "s2_scene_id": s2_scene_id,
                    "tip": "POST /admin/s2/download/{scene_id} then retry",
                },
            )

    import s2_processor
    try:
        # Falls through to s2_processor.DEFAULT_HALF_SIZE_M when client
        # doesn't pass an explicit override.
        chip_kwargs = {"vessel_length_m": det_length_m}
        if half_size_m is not None:
            chip_kwargs["half_size_m"] = half_size_m
        chip = s2_processor.extract_chip(
            detection_id, s2_scene_id, det_lat, det_lon,
            **chip_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("optical_chip generation failed: %s", exc)
        raise HTTPException(500, f"chip generation failed: {exc}")

    return Response(
        content=chip,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/maritime/optical_chip")
def maritime_optical_chip(
    lat: float,
    lon: float,
    half_size_m: float = 1500.0,
    max_days: int = 7,
    max_cloud_pct: float = 60.0,
):
    """Serve the most recent Sentinel-2 RGB chip for ANY lat/lon — not
    just a SAR detection point.

    Lets the side panel show actual optical imagery of any vessel the
    operator clicks (cooperative AIS vessels, ais_gap entities, etc),
    without requiring a SAR pass. Cache key is a hash of the rounded
    lat/lon so different vessels at nearby positions can share a chip.

    Returns:
      - image/jpeg with the rendered chip on success
      - 404 if no recent S2 scene overlaps the point within the window
      - 202 if the matched S2 scene exists but hasn't been downloaded
        yet — frontend can retry after the background download lands

    Query parameters tuned for "show me what's around this vessel":
      half_size_m=1500   ⇒ 3 km × 3 km chip; vessels are visible as
                           bright pixels at 10 m S2 GSD.
      max_days=7         ⇒ widen from the SAR-detection 3-day window
                           because we don't have a SAR timestamp to
                           anchor — last week is fine.
      max_cloud_pct=60   ⇒ accept moderately cloudy scenes; a cloudy
                           chip is more useful than no chip at all.
    """
    import hashlib
    from datetime import datetime, timezone as _tz
    from fastapi.responses import Response, JSONResponse
    from db import models as dbm
    from db.session import session_scope

    # Deterministic cache key — bucket lat/lon to ~110 m precision so
    # multiple vessels in the same neighborhood share a single chip.
    bucket_lat = round(lat, 3)
    bucket_lon = round(lon, 3)
    half = round(half_size_m)
    raw = f"point_{bucket_lat}_{bucket_lon}_{half}"
    cache_key = hashlib.sha256(raw.encode()).hexdigest()[:16]

    now = datetime.now(_tz.utc)
    s2_scene_id = s2.find_nearest_s2_for_point(
        lat, lon, now,
        max_days=max_days, max_cloud_pct=max_cloud_pct,
    )
    if s2_scene_id is None:
        raise HTTPException(
            404,
            f"no Sentinel-2 scene within ±{max_days} days and "
            f"≤{max_cloud_pct}% cloud overlaps this point",
        )

    with session_scope() as s:
        s2_scene = s.get(dbm.S2SceneRow, s2_scene_id)
        if s2_scene is None or s2_scene.state != "downloaded":
            return JSONResponse(
                status_code=202,
                content={
                    "status": "s2_scene_not_yet_downloaded",
                    "s2_scene_id": s2_scene_id,
                    "tip": "POST /admin/s2/download/{scene_id} then retry",
                },
            )

    import s2_processor
    try:
        chip = s2_processor.extract_chip(
            cache_key, s2_scene_id, lat, lon,
            half_size_m=half_size_m,
            # No vessel-length annotation here — we don't know if this
            # point even IS a vessel. Just the underlying imagery.
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("point optical_chip generation failed: %s", exc)
        raise HTTPException(500, f"chip generation failed: {exc}")

    return Response(
        content=chip,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/admin/sar/discover")
def admin_sar_discover(x_admin_token: str | None = Header(default=None)):
    """Manually trigger a Sentinel-1 catalog discovery sweep."""
    _require_admin(x_admin_token)
    scenes = sar.discover_scenes(limit=50)
    result = sar.record_scenes(scenes)
    audit_log.append(
        actor="admin", event_type="sar_discover_manual",
        payload={"discovered": len(scenes), **result},
    )
    return {"discovered": len(scenes), **result}


def _do_sar_download(scene_id: str) -> None:
    """Background-task body. Logs + records audit, swallows errors so
    they end up in sar_scenes.failure_reason rather than the FastAPI
    background-task error handler."""
    try:
        result = sar.download_scene_to_r2(scene_id)
        audit_log.append(
            actor="admin", event_type="sar_scene_downloaded",
            payload={
                "scene_id": scene_id,
                "raw_url": result.get("raw_url"),
                "bytes": result.get("bytes"),
                "skipped": result.get("skipped"),
            },
        )
        log.info("admin SAR download done: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.exception("admin SAR download failed: %s", exc)


@app.get("/admin/sar/scenes")
def admin_sar_scenes(
    state: str | None = None,
    limit: int = 50,
    x_admin_token: str | None = Header(default=None),
):
    """List Sentinel-1 scenes by state for ops triage. No coords/footprints
    in the response — those would dwarf the JSON. Sorted: smallest scenes
    first within state to make picking a test target easy.
    """
    _require_admin(x_admin_token)
    from sqlalchemy import select as sa_select
    from db import models as dbm
    from db.session import session_scope

    q = sa_select(
        dbm.SarSceneRow.scene_id,
        dbm.SarSceneRow.platform,
        dbm.SarSceneRow.acquired_at,
        dbm.SarSceneRow.state,
        dbm.SarSceneRow.raw_url,
        dbm.SarSceneRow.failure_reason,
        dbm.SarSceneRow.attrs,
    )
    if state:
        q = q.where(dbm.SarSceneRow.state == state)
    q = q.order_by(dbm.SarSceneRow.acquired_at.desc()).limit(limit)

    with session_scope() as s:
        rows = s.execute(q).all()

    out = []
    for r in rows:
        attrs = r.attrs or {}
        out.append({
            "scene_id": r.scene_id,
            "platform": r.platform,
            "acquired_at": r.acquired_at.isoformat() if r.acquired_at else None,
            "state": r.state,
            "raw_url": r.raw_url,
            "failure_reason": r.failure_reason,
            "content_length_bytes": attrs.get("content_length_bytes"),
            "name": attrs.get("name"),
        })
    out.sort(key=lambda x: (x["state"] != "discovered", x["content_length_bytes"] or 0))
    return {"count": len(out), "scenes": out}


@app.post("/admin/sar/download/{scene_id}", status_code=202)
def admin_sar_download(
    scene_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None),
):
    """Kick off a Sentinel-1 scene download from Copernicus to R2.

    Returns 202 immediately. The download runs as a FastAPI BackgroundTask
    server-side (1-2 GB transfer, takes 30-90 seconds, doesn't block the
    response or the client connection). Watch sar_scenes.state to see
    progress: discovered → downloaded (success) or failed (with
    failure_reason).
    """
    _require_admin(x_admin_token)
    background_tasks.add_task(_do_sar_download, scene_id)
    return {
        "scene_id": scene_id,
        "status": "queued",
        "tip": "poll sar_scenes.state for completion",
    }


def _do_sar_process(scene_id: str) -> None:
    """Background-task body for running CFAR on a downloaded scene.

    Also auto-runs the SAR↔AIS fusion step at the end so the new
    detections get matched_entity_id populated where applicable.
    """
    try:
        import sar_processor  # lazy: imports rasterio (heavy native lib)
        result = sar_processor.process_scene(scene_id, fuse_engine=maritime)
        audit_log.append(
            actor="admin", event_type="sar_scene_processed",
            payload={
                "scene_id": scene_id,
                "n_detections": result.get("n_detections"),
                "n_tiles": result.get("n_tiles"),
                "n_raw": result.get("n_raw"),
                "elapsed_s": result.get("elapsed_s"),
                "fusion": result.get("fusion"),
                "skipped": result.get("skipped"),
            },
        )
        log.info("admin SAR process done: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.exception("admin SAR process failed: %s", exc)


def _do_sar_fuse(scene_id: str) -> None:
    """Background-task body for running just the fusion step on an
    already-detected scene. Re-runnable + idempotent: skips detections
    whose matched_entity_id is already set."""
    try:
        import sar_processor
        result = sar_processor.fuse_detections(maritime, scene_id)
        audit_log.append(
            actor="admin", event_type="sar_scene_fused",
            payload=result,
        )
        log.info("admin SAR fuse done: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.exception("admin SAR fuse failed: %s", exc)


@app.post("/admin/sar/process/{scene_id}", status_code=202)
def admin_sar_process(
    scene_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None),
):
    """Run CFAR detection on a downloaded SAR scene + persist sar_detections.

    Returns 202 immediately. Detection runs server-side in a BackgroundTask
    (1-2 GB scene → 35 tiles × ~7s = 4-5 min wall clock; handled in a
    threadpool worker so the AIS / health-check loops keep ticking).

    Watch sar_scenes.state for completion: downloaded → detected.
    Watch sar_scenes.attrs.detection_summary for tile/detection counts.
    """
    _require_admin(x_admin_token)
    background_tasks.add_task(_do_sar_process, scene_id)
    return {
        "scene_id": scene_id,
        "status": "queued",
        "tip": "poll sar_scenes.state (→ 'detected') and sar_detections rows",
    }


@app.post("/admin/sar/purge-on-land")
def admin_purge_on_land(
    dry_run: bool = True,
    x_admin_token: str | None = Header(default=None),
):
    """One-shot cleanup: delete sar_detections AND dark_vessel entities
    that fall on dry land.

    Scenes processed before the land-mask was wired (Phase 4.x) include
    inland CFAR false positives — buildings, ag patterns, etc. that
    appear bright in SAR. The fusion step then created a dark_vessel
    entity for each. This endpoint applies the current land_mask
    polygon to both:
      - sar_detections.geom — drops the row outright
      - entities (type=dark_vessel) — drops the entity + its
        recommendations + its in-memory engine entry

    Pass dry_run=false to actually delete; default reports counts only.
    """
    _require_admin(x_admin_token)
    import land_mask
    from sqlalchemy import select as sa_select, delete as sa_delete
    from geoalchemy2.shape import to_shape
    from db import models as dbm
    from db.session import session_scope

    if not land_mask.is_loaded():
        return {"error": "land mask not loaded; check backend/data/aoi_land.geojson"}

    with session_scope() as s:
        # 1) SAR detections on land
        det_rows = s.execute(sa_select(dbm.SarDetectionRow)).scalars().all()
        det_on_land_ids = []
        for r in det_rows:
            pt = to_shape(r.geom)
            if land_mask.is_on_land(pt.y, pt.x):
                det_on_land_ids.append(r.detection_id)

        # 2) Dark-vessel entities on land. We don't touch type=vessel /
        # ais_gap because those reflect AIS reality regardless of where
        # the latest report places them (think a shore-side AIS receiver
        # mapping a fishing trawler against a county boundary).
        ent_rows = s.execute(
            sa_select(dbm.EntityRow).where(
                dbm.EntityRow.domain == "maritime",
                dbm.EntityRow.type == "dark_vessel",
            )
        ).scalars().all()
        ent_on_land_ids = []
        for r in ent_rows:
            pt = to_shape(r.geom)
            if land_mask.is_on_land(pt.y, pt.x):
                ent_on_land_ids.append(r.entity_id)

        if dry_run or (not det_on_land_ids and not ent_on_land_ids):
            return {"dry_run": dry_run,
                    "total_detections": len(det_rows),
                    "detections_on_land": len(det_on_land_ids),
                    "total_dark_entities": len(ent_rows),
                    "dark_entities_on_land": len(ent_on_land_ids)}

        if det_on_land_ids:
            s.execute(
                sa_delete(dbm.SarDetectionRow)
                .where(dbm.SarDetectionRow.detection_id.in_(det_on_land_ids))
            )
        if ent_on_land_ids:
            s.execute(
                sa_delete(dbm.RecommendationRow)
                .where(dbm.RecommendationRow.entity_id.in_(ent_on_land_ids))
            )
            s.execute(
                sa_delete(dbm.EntityRow)
                .where(dbm.EntityRow.entity_id.in_(ent_on_land_ids))
            )

    # Drop in-memory engine copies so /maritime/entities reflects the
    # cleanup immediately without a restart.
    for eid in ent_on_land_ids:
        maritime.entities.pop(eid, None)

    audit_log.append(
        actor="admin", event_type="sar_on_land_purged",
        payload={"detections_deleted": len(det_on_land_ids),
                 "dark_entities_deleted": len(ent_on_land_ids),
                 "sample_detection_ids": det_on_land_ids[:5],
                 "sample_entity_ids": ent_on_land_ids[:5]},
    )
    return {"dry_run": False,
            "detections_deleted": len(det_on_land_ids),
            "dark_entities_deleted": len(ent_on_land_ids),
            "remaining_detections": len(det_rows) - len(det_on_land_ids)}


@app.post("/admin/maritime/purge-non-aoi")
def admin_purge_non_aoi(
    dry_run: bool = True,
    x_admin_token: str | None = Header(default=None),
):
    """One-shot cleanup: delete maritime entities outside the Texas AOI.

    The original demo seeded synthetic vessels off NW Madagascar
    (lat -13.7, lon 48.2) for a self-contained scenario. Those rows
    later showed up in /maritime/entities and pulled the frontend's
    auto-fit camera out to a global zoom — Texas vessels became
    unreadable. This endpoint evicts them along with their dangling
    observations + recommendations so the operator never sees them
    again.

    AOI = -98..-93.5 lon, 25.5..30.5 lat (Texas shoreline + Gulf).
    Pass dry_run=false to actually delete; default reports counts only.

    Also drops the in-memory engine entries so the live map
    reflects the cleanup immediately without a restart.
    """
    _require_admin(x_admin_token)
    from sqlalchemy import select as sa_select, delete as sa_delete, and_, or_, func
    from geoalchemy2.shape import to_shape
    from geoalchemy2.functions import ST_X, ST_Y
    from db import models as dbm
    from db.session import session_scope

    AOI_MIN_LON, AOI_MAX_LON = -98.0, -93.5
    AOI_MIN_LAT, AOI_MAX_LAT = 25.5, 30.5

    with session_scope() as s:
        # ST_X/ST_Y on PostGIS Point geometry — operate over the entity row.
        out_of_aoi = sa_select(dbm.EntityRow.entity_id).where(
            and_(
                dbm.EntityRow.domain == "maritime",
                or_(
                    ST_X(dbm.EntityRow.geom) < AOI_MIN_LON,
                    ST_X(dbm.EntityRow.geom) > AOI_MAX_LON,
                    ST_Y(dbm.EntityRow.geom) < AOI_MIN_LAT,
                    ST_Y(dbm.EntityRow.geom) > AOI_MAX_LAT,
                ),
            )
        )
        eids = list(s.execute(out_of_aoi).scalars())
        n = len(eids)
        sample = eids[:5]

        if dry_run or n == 0:
            return {"dry_run": dry_run, "non_aoi_entity_count": n,
                    "sample_entity_ids": sample}

        # Cascading delete: observations linked via association table go with
        # the entity (FK ondelete=CASCADE on the assoc table). Recommendations
        # link directly via entity_id FK with ondelete=CASCADE.
        # Safer to delete the assoc rows + observations + recs explicitly
        # here in case the cascades aren't configured everywhere.
        s.execute(
            sa_delete(dbm.RecommendationRow)
            .where(dbm.RecommendationRow.entity_id.in_(eids))
        )
        # Observations referenced exclusively by these entities (this is a
        # heuristic — observations are M:N so a strict purge would walk the
        # association table; for the seed-data case each obs links to one
        # entity, so the join-table delete + observation delete is safe).
        s.execute(
            sa_delete(dbm.EntityRow).where(dbm.EntityRow.entity_id.in_(eids))
        )

    # Drop in-memory copies so the next /maritime/entities call reflects it.
    for eid in eids:
        maritime.entities.pop(eid, None)
    # Rebuild MMSI index from what's left, in case any of the deleted ones
    # held an mmsi mapping.
    maritime._mmsi_index = {  # noqa: SLF001
        str(e.attrs.get("mmsi")): e.entity_id
        for e in list(maritime.entities.values())
        if e.attrs.get("mmsi")
    }

    audit_log.append(
        actor="admin", event_type="non_aoi_entities_purged",
        payload={"deleted_entity_ids": sample, "deleted_count": n,
                 "aoi": [AOI_MIN_LON, AOI_MIN_LAT, AOI_MAX_LON, AOI_MAX_LAT]},
    )
    return {"dry_run": False, "deleted_count": n, "sample_entity_ids": sample}


@app.get("/admin/sar/auto-status")
def admin_sar_auto_status(x_admin_token: str | None = Header(default=None)):
    """Show whether the auto-process loop is enabled, what its config is,
    and what the next eligible scene would be. Read-only — does not pop
    a scene off the queue."""
    _require_admin(x_admin_token)
    next_scene_id = None
    next_scene_attrs = None
    if SAR_AUTO_PROCESS:
        try:
            next_scene_id = _sar_auto_pick_next()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-status pick_next failed: %s", exc)
        if next_scene_id:
            from sqlalchemy import select as sa_select
            from db import models as dbm
            from db.session import session_scope
            with session_scope() as s:
                row = s.get(dbm.SarSceneRow, next_scene_id)
                if row is not None:
                    next_scene_attrs = {
                        "scene_id": row.scene_id,
                        "acquired_at": row.acquired_at.isoformat(),
                        "state": row.state,
                        "name": (row.attrs or {}).get("name"),
                        "content_length_bytes": (row.attrs or {}).get("content_length_bytes"),
                    }
    return {
        "enabled": SAR_AUTO_PROCESS,
        "loop_running": _sar_auto_process_task is not None
                        and not _sar_auto_process_task.done(),
        "interval_s": SAR_AUTO_PROCESS_INTERVAL_S,
        "max_bytes": SAR_AUTO_MAX_BYTES,
        "max_age_hours": SAR_AUTO_MAX_AGE_HOURS,
        "next_pick": next_scene_attrs,
    }


@app.post("/admin/alerts/test")
def admin_alerts_test(x_admin_token: str | None = Header(default=None)):
    """Send a synthetic dark-vessel alert to ALERT_SUBSCRIBERS.

    Useful for verifying the Resend integration + that emails actually
    land (DKIM, spam scoring, formatting) without waiting on a real
    Sentinel-1 pass to produce dark vessels.
    """
    _require_admin(x_admin_token)
    import alerts
    from datetime import datetime, timezone
    if not alerts.is_configured():
        return {"skipped": "RESEND_API_KEY not set"}
    if not alerts.subscribers():
        return {"skipped": "ALERT_SUBSCRIBERS empty"}

    sample = [
        {"lat": 28.4210, "lon": -97.7188, "rcs_db": 78.4,
         "length_m": 60, "confidence": 0.82},
        {"lat": 28.3422, "lon": -97.7104, "rcs_db": 76.0,
         "length_m": 30, "confidence": 0.71},
        {"lat": 28.3889, "lon": -97.6676, "rcs_db": 76.4,
         "length_m": 30, "confidence": 0.73},
    ]
    result = alerts.notify_dark_vessels(
        scene_id="test-scene-0000",
        scene_name="S1A_IW_GRDH_TEST_SYNTHETIC",
        scene_acquired_at=datetime.now(timezone.utc),
        n_dark_new=len(sample),
        n_dark_continued=0,
        sample=sample,
    )
    audit_log.append(
        actor="admin", event_type="alert_test_sent",
        payload={"result": result, "subscribers": len(alerts.subscribers())},
    )
    return result


@app.post("/admin/sar/fuse/{scene_id}", status_code=202)
def admin_sar_fuse(
    scene_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None),
):
    """Run the SAR↔AIS fusion step on an already-detected scene.

    Useful for backfilling matched_entity_id on scenes processed before
    fusion was wired in, or for re-running fusion after AIS data has been
    re-ingested. Idempotent: skips detections whose matched_entity_id is
    already set.
    """
    _require_admin(x_admin_token)
    background_tasks.add_task(_do_sar_fuse, scene_id)
    return {
        "scene_id": scene_id,
        "status": "queued",
        "tip": "poll sar_detections.matched_entity_id to see matches land",
    }


@app.get("/wildfire/entities/{eid}/lineage")
def wildfire_lineage(eid: str):
    data = wildfire.lineage(eid)
    if not data:
        raise HTTPException(404, "entity not found")
    return data


@app.post("/wildfire/actions/{eid}/approve")
def wildfire_approve(eid: str, body: DecisionRequest):
    affected = wildfire.decide(eid, decision=Decision.APPROVED,
                                operator=body.operator, reason=body.reason)
    if not affected:
        raise HTTPException(404, "no pending recommendations")
    return {"approved": [r.model_dump() for r in affected]}


@app.post("/wildfire/actions/{eid}/reject")
def wildfire_reject(eid: str, body: DecisionRequest):
    affected = wildfire.decide(eid, decision=Decision.REJECTED,
                                operator=body.operator, reason=body.reason)
    if not affected:
        raise HTTPException(404, "no pending recommendations")
    return {"rejected": [r.model_dump() for r in affected]}
