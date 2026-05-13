"""
Smoke tests — the bare minimum that proves the multi-domain MVP boots,
serves /health, and that the audit chain seeded by the maritime + wildfire
scenarios verifies end-to-end.

These tests use FastAPI's TestClient (in-process), so they don't need a
running server or network access. CI runs them on every push.

Roadmap calls for "even 1 trivial test" in Phase 0 — these are that.
Phase 1 (Postgres) and Phase 2 (real AIS) will need real fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

# main.py uses sibling-module imports (from models import ..., from fusion import ...)
# rather than a package layout, so add backend/ to sys.path before importing.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """Module-scoped client. The `with` block fires FastAPI startup, which
    runs the seed scenarios that populate the audit log + entity store."""
    with TestClient(app) as c:
        yield c


# Minimum audit entries produced by the maritime + wildfire seed
# scenarios alone (no anomaly bleed from other test modules). The
# in-memory audit_log singleton is shared across test files in the
# same pytest session, so other tests that exercise FusionEngine
# (e.g. test_anomalies.py) write extra entries before test_smoke runs.
# Asserting on a floor instead of an exact count keeps these tests
# robust to the test-collection order pytest happens to pick — a real
# seed regression would still drop the count below the floor and
# surface as a failure.
SEED_AUDIT_FLOOR = 1789


def test_health_returns_both_domains(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["domains"]) == {"maritime", "wildfire"}
    assert isinstance(body["audit_head"], str) and len(body["audit_head"]) == 64
    # Floor — see SEED_AUDIT_FLOOR comment above.
    assert body["audit_entries"] >= SEED_AUDIT_FLOOR


def test_audit_chain_verifies(client) -> None:
    r = client.get("/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["first_bad_seq"] is None
    assert body["entry_count"] >= SEED_AUDIT_FLOOR


def test_maritime_returns_dark_vessels_and_gaps(client) -> None:
    r = client.get("/maritime/entities")
    assert r.status_code == 200
    entities = r.json()["entities"]
    assert len(entities) > 0
    types = {e["type"] for e in entities}
    # Maritime seed scenario must produce these — they're the demo's whole point.
    assert "dark_vessel" in types
    assert "ais_gap" in types


def test_wildfire_returns_fire_events(client) -> None:
    r = client.get("/wildfire/entities")
    assert r.status_code == 200
    entities = r.json()["entities"]
    assert len(entities) > 0
    types = {e["type"] for e in entities}
    assert "fire_event" in types


def test_lineage_404_for_unknown_entity(client) -> None:
    r = client.get("/maritime/entities/ent_does_not_exist/lineage")
    assert r.status_code == 404


def test_decision_404_when_no_pending_recs(client) -> None:
    # Reject on a non-existent entity should 404 rather than silently succeed.
    r = client.post(
        "/maritime/actions/ent_nope/reject",
        json={"operator": "test_smoke", "reason": "nonexistent"},
    )
    assert r.status_code == 404
