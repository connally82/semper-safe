"""
Universal data model for the platform.

Everything in the system reduces to four types:
  - Observation:   a single sensor reading at (cell, time)
  - Entity:        a fused real-world thing, backed by N observations
  - Recommendation: a suggested action, with lineage
  - AuditEntry:    hash-chained record of every state change

This file is the contract. Every other layer depends on it.
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------
class SourceType(str, Enum):
    # Maritime + general
    AIS = "ais"
    SAR = "sar"
    OPTICAL = "optical"
    RF = "rf"
    WEATHER = "weather"
    OPERATOR = "operator"
    # Wildfire (Phase 2)
    VIIRS = "viirs"             # 375m thermal from JPSS
    GOES = "goes"               # 5-min geostationary thermal
    GROUND_CAM = "ground_cam"   # ALERTCalifornia-style fixed cameras
    LIGHTNING = "lightning"     # NLDN strikes


class EntityType(str, Enum):
    # Maritime
    VESSEL = "vessel"
    DARK_VESSEL = "dark_vessel"
    AIS_GAP = "ais_gap"
    LOITERING_VESSEL = "loitering_vessel"   # AIS-cooperative but stationary >N hours
    DEBRIS = "debris"
    # Wildfire
    HOTSPOT = "hotspot"             # single thermal anomaly, unconfirmed
    SMOKE_PLUME = "smoke_plume"     # visible-imagery plume detection
    FIRE_EVENT = "fire_event"       # fused: persistent hotspot + smoke + weather
    FALSE_POSITIVE = "false_positive"  # suppressed (industrial flare, etc.)
    # Cross-domain
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    # Maritime
    TASK_SAR_SAT = "task_sar_satellite"
    DISPATCH_PATROL = "dispatch_patrol_aircraft"
    ALERT_COAST_GUARD = "alert_coast_guard"
    # Wildfire
    ALERT_FIRE_DISPATCH = "alert_fire_dispatch"
    REQUEST_AERIAL_RECON = "request_aerial_recon"
    EVACUATION_ADVISORY = "evacuation_advisory"   # recommended only — never auto
    # Cross-domain
    LOG_ONLY = "log_only"


class Decision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ----------------------------------------------------------------------
# Core types
# ----------------------------------------------------------------------
class Geom(BaseModel):
    """Lon/lat point. For MVP we don't carry polygons; production would."""
    lon: float
    lat: float


class Observation(BaseModel):
    """A single sensor reading. Immutable once written."""
    obs_id: str
    source: SourceType
    source_id: str                # e.g. AIS MMSI, SAR pass ID
    geom: Geom
    h3_cell: str                  # H3 index at resolution 8
    t: datetime
    attrs: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0       # source-reported confidence
    raw_lineage: str | None = None  # URL or pointer to raw data


class Entity(BaseModel):
    """A fused real-world thing. Built from >=1 observations."""
    entity_id: str
    type: EntityType
    geom: Geom                    # most recent best-estimate position
    last_seen: datetime
    first_seen: datetime
    confidence: float             # fused confidence in identification
    priority_score: float         # 0..1, derived; drives operator queue
    observation_ids: list[str] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class Recommendation(BaseModel):
    """A suggested action — never autonomous."""
    rec_id: str
    entity_id: str
    action: ActionType
    rationale: str                # human-readable, derived from lineage
    evidence_obs_ids: list[str]   # which observations triggered this
    suggested_at: datetime
    decision: Decision = Decision.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


class AuditEntry(BaseModel):
    """One hash-chained record. Append-only."""
    seq: int
    t: datetime
    actor: str                    # 'system' or operator name
    event_type: str               # 'observation_added', 'entity_fused', 'recommendation_made', 'decision', etc.
    payload: dict[str, Any]
    prev_hash: str
    self_hash: str
