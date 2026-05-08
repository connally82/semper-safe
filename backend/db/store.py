"""
Repository layer that bridges Pydantic models (the API contract) and
the ORM tables (the storage layer).

The engines (FusionEngine, WildfireFusion) keep an in-memory dict of
entities for fast queries, but write through to Postgres on every
mutation. On startup, the engines call `load_state(domain)` to fill the
in-memory dicts from whatever's already in the DB.

When DATABASE_URL is unset, all functions in this module are no-ops —
the engines stay purely in-memory (used by pytest + local dev w/o a DB).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import (
    Decision,
    Entity,
    EntityType,
    Geom,
    Observation,
    Recommendation,
    SourceType,
)


# Module-level override used by the seed pipeline. When True, put_* are no-ops
# even if DATABASE_URL is set. The seed runs the engines in pure in-memory
# mode and then calls bulk_seed_state once at the end (one transaction).
_force_in_memory = False


def is_persistent() -> bool:
    """True if the store should write to / read from Postgres."""
    if _force_in_memory:
        return False
    return bool(os.environ.get("DATABASE_URL"))


@contextmanager
def disable_persistence() -> Iterator[None]:
    """Suppress all put_* writes within this block. Used by the seed pipeline
    so the engines mutate in-memory only; bulk_seed_state flushes after."""
    global _force_in_memory
    prev = _force_in_memory
    _force_in_memory = True
    try:
        yield
    finally:
        _force_in_memory = prev


# --- Conversions Pydantic ↔ ORM ----------------------------------------

def _geom_to_wkt(g: Geom):
    """GeoAlchemy2 wants a Shapely shape (or WKBElement) — wrap as Point."""
    return from_shape(Point(g.lon, g.lat), srid=4326)


def _geom_from_orm(orm_geom) -> Geom:
    """Convert WKBElement back to Pydantic Geom."""
    p = to_shape(orm_geom)
    return Geom(lon=float(p.x), lat=float(p.y))


# --- Observations -------------------------------------------------------

def put_observation(obs: Observation, *, domain: str) -> None:
    if not is_persistent():
        return
    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        stmt = pg_insert(dbm.ObservationRow).values(
            obs_id=obs.obs_id,
            domain=domain,
            source=obs.source.value,
            source_id=obs.source_id,
            geom=_geom_to_wkt(obs.geom),
            h3_cell=obs.h3_cell,
            t=obs.t,
            attrs=obs.attrs,
            confidence=obs.confidence,
            raw_lineage=obs.raw_lineage,
        ).on_conflict_do_nothing(index_elements=["obs_id"])
        s.execute(stmt)


# --- Entities -----------------------------------------------------------

def put_entity(ent: Entity, *, domain: str) -> None:
    if not is_persistent():
        return
    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        # Upsert the entity
        values = {
            "entity_id": ent.entity_id,
            "domain": domain,
            "type": ent.type.value,
            "geom": _geom_to_wkt(ent.geom),
            "first_seen": ent.first_seen,
            "last_seen": ent.last_seen,
            "confidence": ent.confidence,
            "priority_score": ent.priority_score,
            "attrs": ent.attrs,
            "notes": ent.notes,
        }
        stmt = pg_insert(dbm.EntityRow).values(**values)
        update_cols = {k: stmt.excluded[k] for k in values if k != "entity_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["entity_id"], set_=update_cols)
        s.execute(stmt)

        # Sync entity_observations: add any new (entity_id, obs_id) pairs.
        if ent.observation_ids:
            link_stmt = pg_insert(dbm.entity_observations).values([
                {"entity_id": ent.entity_id, "obs_id": oid}
                for oid in ent.observation_ids
            ]).on_conflict_do_nothing()
            s.execute(link_stmt)


# --- Recommendations ----------------------------------------------------

def put_recommendation(rec: Recommendation) -> None:
    if not is_persistent():
        return
    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        values = {
            "rec_id": rec.rec_id,
            "entity_id": rec.entity_id,
            "action": rec.action.value,
            "rationale": rec.rationale,
            "suggested_at": rec.suggested_at,
            "decision": rec.decision.value,
            "decided_by": rec.decided_by,
            "decided_at": rec.decided_at,
            "decision_reason": rec.decision_reason,
        }
        stmt = pg_insert(dbm.RecommendationRow).values(**values)
        update_cols = {k: stmt.excluded[k] for k in values if k != "rec_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["rec_id"], set_=update_cols)
        s.execute(stmt)

        if rec.evidence_obs_ids:
            ev_stmt = pg_insert(dbm.recommendation_evidence).values([
                {"rec_id": rec.rec_id, "obs_id": oid}
                for oid in rec.evidence_obs_ids
            ]).on_conflict_do_nothing()
            s.execute(ev_stmt)


# --- Loaders -----------------------------------------------------------

class LoadedState(NamedTuple):
    observations: dict[str, Observation]
    entities: dict[str, Entity]
    recommendations: dict[str, Recommendation]


def is_empty(domain: str) -> bool:
    """True if no entities exist for the given domain. Used to gate seeding."""
    if not is_persistent():
        return True
    from db import models as dbm
    from db.session import session_scope
    from sqlalchemy import select, func

    with session_scope() as s:
        n = s.execute(
            select(func.count())
            .select_from(dbm.EntityRow)
            .where(dbm.EntityRow.domain == domain)
        ).scalar_one()
        return n == 0


def load_entities_only(domain: str) -> LoadedState:
    """Fast-path startup load: entities only, no observations/recommendations.

    The full load_state walks the entity↔observation many-to-many association
    eagerly. After ~24h of AIS ingest the join blows up to 100k+ rows and
    times out FastAPI's startup handler before uvicorn declares itself ready.

    The engine doesn't actually need observations preloaded:
      - AIS ingest only consults _mmsi_index (built from entities) to dedup
      - /maritime/entities/{eid}/track and /maritime/timeline already query DB
      - Lineage endpoints can fall back to DB lookups (or be lazified)

    So at boot we load only the entities + recommendations (small) and let
    observations grow incrementally as they're ingested. This drops startup
    from ~80s+ (timing out) to ~3s on a populated DB.
    """
    if not is_persistent():
        return LoadedState({}, {}, {})

    from db import models as dbm
    from db.session import session_scope
    from sqlalchemy import select
    from sqlalchemy.orm import noload

    with session_scope() as s:
        # noload() on the observations relationship: don't even fire the
        # selectin query for the join. observation_ids on Entity stays empty
        # (gets repopulated as observations land via ingest paths).
        ent_rows = s.execute(
            select(dbm.EntityRow)
            .where(dbm.EntityRow.domain == domain)
            .options(noload(dbm.EntityRow.observations))
        ).scalars().all()
        entities: dict[str, Entity] = {}
        for r in ent_rows:
            entities[r.entity_id] = Entity(
                entity_id=r.entity_id,
                type=EntityType(r.type),
                geom=_geom_from_orm(r.geom),
                last_seen=r.last_seen,
                first_seen=r.first_seen,
                confidence=r.confidence,
                priority_score=r.priority_score,
                observation_ids=[],     # populated incrementally on ingest
                attrs=r.attrs,
                notes=r.notes,
            )

        # Recommendations are still small — load them so /decisions endpoints
        # work without an extra round-trip.
        rec_rows = s.execute(
            select(dbm.RecommendationRow)
            .join(dbm.EntityRow, dbm.RecommendationRow.entity_id == dbm.EntityRow.entity_id)
            .where(dbm.EntityRow.domain == domain)
            .options(noload(dbm.RecommendationRow.evidence))
        ).scalars().all()
        recommendations: dict[str, Recommendation] = {}
        for r in rec_rows:
            from models import ActionType
            recommendations[r.rec_id] = Recommendation(
                rec_id=r.rec_id,
                entity_id=r.entity_id,
                action=ActionType(r.action),
                rationale=r.rationale,
                evidence_obs_ids=[],   # lazy, /lineage queries DB on demand
                suggested_at=r.suggested_at,
                decision=Decision(r.decision),
                decided_by=r.decided_by,
                decided_at=r.decided_at,
                decision_reason=r.decision_reason,
            )

        return LoadedState(
            observations={},   # incrementally populated via ingest
            entities=entities,
            recommendations=recommendations,
        )


def load_state(domain: str) -> LoadedState:
    """Load full in-memory state for a domain. Empty if no DB or no rows."""
    if not is_persistent():
        return LoadedState({}, {}, {})

    from db import models as dbm
    from db.session import session_scope
    from sqlalchemy import select

    with session_scope() as s:
        # Entities for this domain (selectin loads observation links eagerly)
        ent_rows = s.execute(
            select(dbm.EntityRow).where(dbm.EntityRow.domain == domain)
        ).scalars().all()
        entities: dict[str, Entity] = {}
        observation_ids_per_entity: dict[str, list[str]] = {}
        for r in ent_rows:
            obs_ids = [o.obs_id for o in r.observations]
            observation_ids_per_entity[r.entity_id] = obs_ids
            entities[r.entity_id] = Entity(
                entity_id=r.entity_id,
                type=EntityType(r.type),
                geom=_geom_from_orm(r.geom),
                last_seen=r.last_seen,
                first_seen=r.first_seen,
                confidence=r.confidence,
                priority_score=r.priority_score,
                observation_ids=obs_ids,
                attrs=r.attrs,
                notes=r.notes,
            )

        # Observations referenced by any of the loaded entities. We could
        # filter by domain on observations directly, but this is simpler
        # and matches the in-memory semantics (engine only knows about
        # observations it ingested for its domain).
        all_obs_ids = {oid for ids in observation_ids_per_entity.values() for oid in ids}
        observations: dict[str, Observation] = {}
        if all_obs_ids:
            obs_rows = s.execute(
                select(dbm.ObservationRow).where(dbm.ObservationRow.obs_id.in_(all_obs_ids))
            ).scalars().all()
            for r in obs_rows:
                observations[r.obs_id] = Observation(
                    obs_id=r.obs_id,
                    source=SourceType(r.source),
                    source_id=r.source_id,
                    geom=_geom_from_orm(r.geom),
                    h3_cell=r.h3_cell,
                    t=r.t,
                    attrs=r.attrs,
                    confidence=r.confidence,
                    raw_lineage=r.raw_lineage,
                )

        # Recommendations whose entity is in this domain.
        rec_rows = s.execute(
            select(dbm.RecommendationRow)
            .join(dbm.EntityRow, dbm.RecommendationRow.entity_id == dbm.EntityRow.entity_id)
            .where(dbm.EntityRow.domain == domain)
        ).scalars().all()
        recommendations: dict[str, Recommendation] = {}
        for r in rec_rows:
            from models import ActionType
            recommendations[r.rec_id] = Recommendation(
                rec_id=r.rec_id,
                entity_id=r.entity_id,
                action=ActionType(r.action),
                rationale=r.rationale,
                evidence_obs_ids=[ev.obs_id for ev in r.evidence],
                suggested_at=r.suggested_at,
                decision=Decision(r.decision),
                decided_by=r.decided_by,
                decided_at=r.decided_at,
                decision_reason=r.decision_reason,
            )

        return LoadedState(
            observations=observations,
            entities=entities,
            recommendations=recommendations,
        )


# --- Bulk seed flush ---------------------------------------------------

def bulk_seed_state(
    *,
    observations: Iterable[Observation],
    entities: Iterable[Entity],
    recommendations: Iterable[Recommendation],
    domain: str,
) -> dict[str, int]:
    """Bulk-insert seed state in a SINGLE transaction. Used once per domain
    after engines run in disable_persistence() mode.

    Per-call put_* would do ~3-5 round trips * ~1800 calls = 5-9 minutes
    against Neon from Fly. This collapses it to ~4 round trips total
    (one INSERT per table) → ~5 seconds.

    Returns a counts dict for the audit summary entry the caller should write.
    """
    if not is_persistent():
        return {"observations": 0, "entities": 0,
                "recommendations": 0, "entity_observations": 0,
                "recommendation_evidence": 0}

    from db import models as dbm
    from db.session import session_scope

    obs_rows = [
        {
            "obs_id": o.obs_id,
            "domain": domain,
            "source": o.source.value,
            "source_id": o.source_id,
            "geom": _geom_to_wkt(o.geom),
            "h3_cell": o.h3_cell,
            "t": o.t,
            "attrs": o.attrs,
            "confidence": o.confidence,
            "raw_lineage": o.raw_lineage,
        }
        for o in observations
    ]
    ent_list = list(entities)
    ent_rows = [
        {
            "entity_id": e.entity_id,
            "domain": domain,
            "type": e.type.value,
            "geom": _geom_to_wkt(e.geom),
            "first_seen": e.first_seen,
            "last_seen": e.last_seen,
            "confidence": e.confidence,
            "priority_score": e.priority_score,
            "attrs": e.attrs,
            "notes": e.notes,
        }
        for e in ent_list
    ]
    link_rows = [
        {"entity_id": e.entity_id, "obs_id": oid}
        for e in ent_list
        for oid in e.observation_ids
    ]
    rec_list = list(recommendations)
    rec_rows = [
        {
            "rec_id": r.rec_id,
            "entity_id": r.entity_id,
            "action": r.action.value,
            "rationale": r.rationale,
            "suggested_at": r.suggested_at,
            "decision": r.decision.value,
            "decided_by": r.decided_by,
            "decided_at": r.decided_at,
            "decision_reason": r.decision_reason,
        }
        for r in rec_list
    ]
    rec_ev_rows = [
        {"rec_id": r.rec_id, "obs_id": oid}
        for r in rec_list
        for oid in r.evidence_obs_ids
    ]

    with session_scope() as s:
        # Order matters: parents before children (FK constraints).
        if obs_rows:
            s.execute(
                pg_insert(dbm.ObservationRow)
                .values(obs_rows)
                .on_conflict_do_nothing(index_elements=["obs_id"])
            )
        if ent_rows:
            s.execute(
                pg_insert(dbm.EntityRow)
                .values(ent_rows)
                .on_conflict_do_nothing(index_elements=["entity_id"])
            )
        if link_rows:
            s.execute(
                pg_insert(dbm.entity_observations)
                .values(link_rows)
                .on_conflict_do_nothing()
            )
        if rec_rows:
            s.execute(
                pg_insert(dbm.RecommendationRow)
                .values(rec_rows)
                .on_conflict_do_nothing(index_elements=["rec_id"])
            )
        if rec_ev_rows:
            s.execute(
                pg_insert(dbm.recommendation_evidence)
                .values(rec_ev_rows)
                .on_conflict_do_nothing()
            )

    return {
        "observations": len(obs_rows),
        "entities": len(ent_rows),
        "entity_observations": len(link_rows),
        "recommendations": len(rec_rows),
        "recommendation_evidence": len(rec_ev_rows),
    }


# --- Track query ------------------------------------------------------

def load_timeline(domain: str, *, at: datetime, lookback_minutes: int = 60,
                  limit: int = 1000) -> list[dict]:
    """Return each entity's most-recent position at time `at` (latest
    observation ≤ at), filtered to vessels that have reported within
    `lookback_minutes` before `at` so we don't carry stale ghosts.

    Used by the time-scrub UI: slide the time slider back N minutes and
    see the world state at that instant.

    Returns list of {entity_id, type, lon, lat, t, name, mmsi, priority_score}
    suitable for direct JSON serialization. Querying observations directly
    (rather than entities.geom) means we get the historical position, not
    the current one.
    """
    if not is_persistent():
        return []
    from sqlalchemy import text

    from db.session import session_scope

    window_start = at - timedelta(minutes=lookback_minutes)
    sql = text("""
        SELECT DISTINCT ON (eo.entity_id)
            eo.entity_id,
            ent.type,
            ent.priority_score,
            ent.attrs,
            o.t,
            ST_X(o.geom::geometry) AS lon,
            ST_Y(o.geom::geometry) AS lat
        FROM observations o
        JOIN entity_observations eo ON eo.obs_id = o.obs_id
        JOIN entities ent          ON ent.entity_id = eo.entity_id
        WHERE ent.domain = :domain
          AND o.t <= :at
          AND o.t >= :window_start
        ORDER BY eo.entity_id, o.t DESC
        LIMIT :limit
    """)
    with session_scope() as s:
        rows = s.execute(sql, {
            "domain": domain,
            "at": at,
            "window_start": window_start,
            "limit": limit,
        }).all()
        out = []
        for entity_id, etype, priority, attrs, t, lon, lat in rows:
            attrs = attrs or {}
            out.append({
                "entity_id": entity_id,
                "type": etype,
                "priority_score": float(priority) if priority is not None else 0.0,
                "name": attrs.get("name"),
                "mmsi": attrs.get("mmsi"),
                "lon": float(lon),
                "lat": float(lat),
                "t": t.isoformat(),
            })
        return out


def load_track(eid: str, *, limit: int = 200) -> list[Observation]:
    """Return the most-recent `limit` observations for an entity, in time
    order (oldest first). Used by the /track endpoint so older entities
    that have been evicted from the in-memory cache still show their
    full Postgres-stored history.

    Returns [] when DATABASE_URL is unset (caller falls back to in-memory).
    """
    if not is_persistent():
        return []
    from sqlalchemy import select

    from db import models as dbm
    from db.session import session_scope

    with session_scope() as s:
        rows = s.execute(
            select(dbm.ObservationRow)
            .join(
                dbm.entity_observations,
                dbm.ObservationRow.obs_id == dbm.entity_observations.c.obs_id,
            )
            .where(dbm.entity_observations.c.entity_id == eid)
            .order_by(dbm.ObservationRow.t.desc())
            .limit(limit)
        ).scalars().all()
        # Pull data into Pydantic objects while the session is live —
        # session_scope closes on exit and detaches the ORM rows.
        return list(reversed([
            Observation(
                obs_id=r.obs_id,
                source=SourceType(r.source),
                source_id=r.source_id,
                geom=_geom_from_orm(r.geom),
                h3_cell=r.h3_cell,
                t=r.t,
                attrs=r.attrs,
                confidence=r.confidence,
                raw_lineage=r.raw_lineage,
            )
            for r in rows
        ]))


# --- Retention --------------------------------------------------------

def purge_old_observations(*, older_than_hours: int) -> int:
    """Delete observations whose t is older than the cutoff. CASCADE on
    entity_observations + recommendation_evidence cleans up FK links
    automatically (set in the initial migration).

    Audit log is intentionally NOT touched — it's the load-bearing
    inspectability surface per docs/blueprint.md, and audit entries
    that reference deleted observations are still meaningful (you can
    see WHEN something happened even if the raw obs row is gone).

    Returns the number of observations deleted.

    Background: at ~9 AIS events/sec the observations + audit + link
    tables together fill Neon's 0.5 GB free tier in ~15 hours. A
    24-hour TTL keeps tracks meaningful (vessels' recent paths) while
    bounding storage at ~1 day's worth of raw data.
    """
    if not is_persistent():
        return 0
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be positive")

    from sqlalchemy import text

    from db.session import session_scope

    with session_scope() as s:
        result = s.execute(
            text(
                "DELETE FROM observations "
                "WHERE t < (NOW() AT TIME ZONE 'UTC') - (:hours || ' hours')::interval"
            ),
            {"hours": older_than_hours},
        )
    return result.rowcount or 0


# Heuristic guard: shapely is needed for from_shape/to_shape; ensure import
# fails loudly at module load rather than at first call.
def _import_check() -> None:
    _ = (Point, from_shape, to_shape, datetime)


_import_check()
