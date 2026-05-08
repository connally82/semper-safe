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
                evicted = await asyncio.to_thread(_evict_stale_in_memory_obs)
                if evicted:
                    log.debug("evicted %d stale in-memory observations", evicted)
            except Exception as exc:  # noqa: BLE001
                log.exception("in-memory eviction crashed: %s", exc)


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

    Sync helper — call from a thread via asyncio.to_thread.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as sa_select, and_
    from db import models as dbm
    from db.session import session_scope

    cutoff = datetime.now(timezone.utc) - timedelta(hours=SAR_AUTO_MAX_AGE_HOURS)
    with session_scope() as s:
        # We can't filter by content_length_bytes in SQL because it's
        # nested in attrs JSONB. Pull a small candidate set and filter
        # in Python — eligible scenes are usually <10 per tick.
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


@app.get("/health")
def health():
    return {
        "ok": True,
        "domains": ["maritime", "wildfire"],
        "audit_head": audit_log.head(),
        "audit_entries": len(audit_log.all()),
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


# Wildfire domain
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
        chip_kwargs = {}
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
