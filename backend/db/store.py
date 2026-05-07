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
from datetime import datetime
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


def is_persistent() -> bool:
    """True if the store should write to / read from Postgres."""
    return bool(os.environ.get("DATABASE_URL"))


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


# Heuristic guard: shapely is needed for from_shape/to_shape; ensure import
# fails loudly at module load rather than at first call.
def _import_check() -> None:
    _ = (Point, from_shape, to_shape, datetime)


_import_check()
