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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import Decision
from fusion import FusionEngine
from wildfire import WildfireFusion
from audit import audit_log
from db import store
from seed_data import build_scenario, SCENARIO_START as MARITIME_START
from wildfire_seed import build_wildfire_scenario

# Phase 2: real-time AIS ingestion (optional — only runs if API key is set)
import aisstream

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

# How often to scan maritime entities for AIS dropouts. The sweep is cheap
# (in-memory iteration over self.entities); the real cost is the audit/store
# writes for any newly-flagged gaps. 60s is a reasonable default — a 30s
# sweep would surface dropouts faster but double the audit churn.
GAP_SWEEP_INTERVAL_S = 60


async def _gap_sweeper_loop(cancel: asyncio.Event) -> None:
    """Periodic AIS-gap detection. Replaces the per-ingest detect_gaps
    calls that the synthetic seed used — real-time AIS arrives one
    message at a time, so dropouts only surface from a sweep."""
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
        global _aisstream_task, _aisstream_cancel, _gap_sweeper_task, _gap_sweeper_cancel
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
    else:
        log.info("AISSTREAM_API_KEY not set; skipping AIS ingestion + gap sweep")


@app.on_event("shutdown")
async def _shutdown():
    global _aisstream_task, _aisstream_cancel, _gap_sweeper_task, _gap_sweeper_cancel
    for cancel in (_aisstream_cancel, _gap_sweeper_cancel):
        if cancel is not None:
            cancel.set()
    for task in (_aisstream_task, _gap_sweeper_task):
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
    rows = sorted(maritime.entities.values(),
                  key=lambda e: (-e.priority_score, -e.last_seen.timestamp()))
    return {"entities": [e.model_dump() for e in rows]}


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
    rows = sorted(wildfire.entities.values(),
                  key=lambda e: (-e.priority_score, -e.last_seen.timestamp()))
    return {"entities": [e.model_dump() for e in rows]}


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
