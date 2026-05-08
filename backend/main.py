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

from fastapi import FastAPI, Header, HTTPException
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
    skip_seed = os.environ.get("SKIP_SEED") == "1"
    persistent = store.is_persistent()

    maritime_empty = persistent and store.is_empty("maritime")
    wildfire_empty = persistent and store.is_empty("wildfire")

    if persistent and not maritime_empty:
        maritime.load_persisted_state()
    if persistent and not wildfire_empty:
        wildfire.load_persisted_state()

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
    else:
        log.info("AISSTREAM_API_KEY not set; skipping AIS ingestion + gap sweep + retention")


@app.on_event("shutdown")
async def _shutdown():
    global _aisstream_task, _aisstream_cancel
    global _gap_sweeper_task, _gap_sweeper_cancel
    global _retention_task, _retention_cancel
    global _audit_archive_task, _audit_archive_cancel
    global _sar_discover_task, _sar_discover_cancel
    for cancel in (_aisstream_cancel, _gap_sweeper_cancel,
                   _retention_cancel, _audit_archive_cancel,
                   _sar_discover_cancel):
        if cancel is not None:
            cancel.set()
    for task in (_aisstream_task, _gap_sweeper_task,
                 _retention_task, _audit_archive_task,
                 _sar_discover_task):
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


@app.post("/admin/sar/download/{scene_id}")
def admin_sar_download(
    scene_id: str,
    x_admin_token: str | None = Header(default=None),
):
    """Manually download a Sentinel-1 scene from Copernicus to R2.

    Runs synchronously; takes 30-90 seconds for a 1-2 GB scene over Fly's
    network. Updates sar_scenes state machine.
    """
    _require_admin(x_admin_token)
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
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("admin SAR download failed: %s", exc)
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


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
