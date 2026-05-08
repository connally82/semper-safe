"""
ORM tables. Mirror the Pydantic types in ../models.py.

Mapping:
  Observation     → ObservationRow
  Entity          → EntityRow         (M:N to ObservationRow via entity_observations)
  Recommendation  → RecommendationRow (M:N to ObservationRow via recommendation_evidence)
  AuditEntry      → AuditEntryRow     (chain integrity enforced by DB)

Phase 1 adds a `domain` column to entities/observations/recommendations because
the platform now hosts multiple domains (maritime, wildfire, ...). Audit log
remains domain-agnostic — one chain audits the whole platform per the
'civilian inspectability' principle in docs/blueprint.md.
"""

from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


# Domain is a small fixed enum stored as a short string; using a CHECK constraint
# rather than a PG ENUM type so we can extend it without an `ALTER TYPE` migration.
_DOMAIN_VALUES = ("maritime", "wildfire", "anti_poaching", "wilderness_sar", "amber",
                  "humanitarian", "flood", "cross")


# --- Association tables -------------------------------------------------

entity_observations = Table(
    "entity_observations",
    Base.metadata,
    Column("entity_id", String, ForeignKey("entities.entity_id", ondelete="CASCADE"),
           primary_key=True),
    Column("obs_id", String, ForeignKey("observations.obs_id", ondelete="CASCADE"),
           primary_key=True),
)


recommendation_evidence = Table(
    "recommendation_evidence",
    Base.metadata,
    Column("rec_id", String, ForeignKey("recommendations.rec_id", ondelete="CASCADE"),
           primary_key=True),
    Column("obs_id", String, ForeignKey("observations.obs_id", ondelete="CASCADE"),
           primary_key=True),
)


# --- Tables --------------------------------------------------------------

class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint(
            "domain IN " + str(_DOMAIN_VALUES).replace(",)", ")"),
            name="observations_domain_check",
        ),
        Index("ix_observations_t", "t"),
        Index("ix_observations_source", "source"),
        Index("ix_observations_geom", "geom", postgresql_using="gist"),
    )

    obs_id: Mapped[str] = mapped_column(String, primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    geom = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    h3_cell: Mapped[str] = mapped_column(String(20), nullable=False)
    t: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    raw_lineage: Mapped[str | None] = mapped_column(String, nullable=True)


class EntityRow(Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "domain IN " + str(_DOMAIN_VALUES).replace(",)", ")"),
            name="entities_domain_check",
        ),
        Index("ix_entities_priority", "priority_score"),
        Index("ix_entities_last_seen", "last_seen"),
        Index("ix_entities_geom", "geom", postgresql_using="gist"),
        Index("ix_entities_domain_type", "domain", "type"),
    )

    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    geom = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(String, nullable=False, default="")

    observations: Mapped[list[ObservationRow]] = relationship(
        secondary=entity_observations, lazy="selectin",
    )


class RecommendationRow(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('pending','approved','rejected')",
            name="recommendations_decision_check",
        ),
        Index("ix_recommendations_entity", "entity_id"),
        Index("ix_recommendations_decision", "decision"),
    )

    rec_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.entity_id", ondelete="CASCADE"), nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    suggested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    evidence: Mapped[list[ObservationRow]] = relationship(
        secondary=recommendation_evidence, lazy="selectin",
    )
    entity: Mapped["EntityRow"] = relationship(lazy="joined")


class ArchiveStateRow(Base):
    """Tiny key/value table for the audit cold-archive job.

    Single row keyed by `name='audit'` tracks the highest seq we've copied
    to R2 so the next run is a delta-only push. Schema-versioned in case
    we add other archives later (observations? recommendations?).
    """

    __tablename__ = "archive_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=-1)
    last_archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class AuditEntryRow(Base):
    """Hash-chained audit log.

    Integrity is enforced at the DB layer, not just in code:
      - PK on seq:           sequence numbers can't be reused
      - UNIQUE on prev_hash: the chain can't fork
      - UNIQUE on self_hash: each entry is unique by content
    Tampering with any past entry breaks one of these constraints on the next
    write OR the chain stops verifying.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("prev_hash", name="audit_log_prev_hash_unique"),
        UniqueConstraint("self_hash", name="audit_log_self_hash_unique"),
        Index("ix_audit_log_t", "t"),
        Index("ix_audit_log_actor", "actor"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    t: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False)
