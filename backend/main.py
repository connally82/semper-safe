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


app = FastAPI(title="Semper Safe — Multi-Domain")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)

maritime = FusionEngine()
wildfire = WildfireFusion()


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
    """Phase 1 startup:
       - If a DB is configured AND already has entities for a domain, load
         them in-memory and skip the seed scenario for that domain.
       - Otherwise, run the seed scenario (which now also writes through
         to the DB on every ingest, when DATABASE_URL is set).

    The audit log is shared across runs when DB-backed, so a "process_started"
    entry on every cold-boot is what surfaces restarts in the chain — exactly
    what the Phase 1 exit criterion calls for.
    """
    audit_log.append(actor="system", event_type="process_started",
                     payload={"persistent": store.is_persistent()})

    if store.is_persistent() and not store.is_empty("maritime"):
        maritime.load_persisted_state()
        audit_log.append(actor="system", event_type="domain_resumed",
                         payload={"domain": "maritime",
                                  "entities": len(maritime.entities)})
    else:
        _seed_maritime()

    if store.is_persistent() and not store.is_empty("wildfire"):
        wildfire.load_persisted_state()
        audit_log.append(actor="system", event_type="domain_resumed",
                         payload={"domain": "wildfire",
                                  "entities": len(wildfire.entities)})
    else:
        _seed_wildfire()


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
