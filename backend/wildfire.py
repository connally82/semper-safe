"""
Wildfire domain plugin (Phase 2).

Pluggability principle: this file is additive. It does not modify
models.py, audit.py, or the FusionEngine in fusion.py — except that
Phase 2 added new enum values to those existing types, which is
exactly the extension point the platform exposes.

This module provides:
  - WildfireFusion: domain-specific fusion rules
  - Recommendation playbooks for fire response
  - The same lineage + audit guarantees as Phase 1

Operational pattern:
  hotspot pixel(s)  ─┐
  smoke plume       ─┼──► fire_event entity ──► recommendation
  weather context   ─┤                            (operator approves)
  WUI proximity     ─┘
"""

from __future__ import annotations
import uuid
from datetime import datetime, timedelta

from models import (
    Observation, Entity, EntityType, SourceType,
    Recommendation, ActionType, Decision, Geom,
)
from audit import audit_log
from db import store
from fusion import haversine_km   # reuse from core


DOMAIN = "wildfire"


# ----------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------
HOTSPOT_PERSISTENCE_WINDOW = timedelta(minutes=20)
HOTSPOT_CLUSTER_KM = 1.0
SMOKE_HOTSPOT_RADIUS_KM = 5.0
SMOKE_HOTSPOT_WINDOW = timedelta(minutes=30)

# Lightning-ignition fusion. A fresh thermal pixel inside this radius
# of a recent CG strike + inside a high-HDW cell is the textbook
# "dry-lightning starts" signal — promote to LIGHTNING_IGNITION_RISK
# with elevated priority. The engine pulls the lightning strike list
# and risk grid from main._wildfire_cache at fusion time.
LIGHTNING_FUSION_RADIUS_KM = 10.0
LIGHTNING_FUSION_WINDOW = timedelta(hours=3)
LIGHTNING_FUSION_RISK_THRESHOLD = 0.5    # HDW risk_score floor


def _lightning_near(lon: float, lat: float, t: datetime) -> dict | None:
    """Return the closest recent lightning strike within fusion bounds,
    or None. Lazy-imports main to avoid an import cycle."""
    try:
        from main import _wildfire_cache
    except Exception:  # noqa: BLE001
        return None
    strikes = _wildfire_cache.get("lightning") or []
    cutoff = t - LIGHTNING_FUSION_WINDOW
    best, best_km = None, float("inf")
    for s in strikes:
        try:
            s_t = datetime.fromisoformat(s["t"])
        except Exception:  # noqa: BLE001
            continue
        if s_t < cutoff:
            continue
        d = haversine_km(
            Geom(lon=lon, lat=lat),
            Geom(lon=s["lon"], lat=s["lat"]),
        )
        if d < best_km and d <= LIGHTNING_FUSION_RADIUS_KM:
            best, best_km = s, d
    return best


def _hdw_risk_at(lon: float, lat: float) -> float:
    """Return the HDW risk_score at the nearest grid cell."""
    try:
        from main import _wildfire_cache
    except Exception:  # noqa: BLE001
        return 0.0
    cells = _wildfire_cache.get("risk_grid") or []
    if not cells:
        return 0.0
    best, best_km = 0.0, float("inf")
    for c in cells:
        d = haversine_km(
            Geom(lon=lon, lat=lat),
            Geom(lon=c["lon"], lat=c["lat"]),
        )
        if d < best_km:
            best = c.get("risk_score") or 0.0
            best_km = d
    return best


# (Geom is already imported at top.)


# Known false-positive sources (industrial flares, gas wells, kilns).
# In production this is a curated geospatial layer; here, two examples.
KNOWN_FP_SOURCES: list[tuple[float, float, float, str]] = [
    # (lon, lat, radius_km, label)
    (-121.890, 38.020, 0.5, "Martinez refinery flare stack"),
    (-119.245, 35.380, 0.3, "Kern Co. gas processing"),
]

# Wildland-urban interface (WUI) markers — proximity escalates response.
# Production: parcel/structure layer from CAL FIRE FRAP.
WUI_ZONES: list[tuple[float, float, float, str]] = [
    (-122.625, 38.480, 6.0, "Santa Rosa WUI"),
    (-121.080, 38.690, 5.0, "Paradise WUI"),
]


# ----------------------------------------------------------------------
# Fusion engine — same shape as maritime, different rules
# ----------------------------------------------------------------------
class WildfireFusion:
    def __init__(self) -> None:
        self.observations: dict[str, Observation] = {}
        self.entities: dict[str, Entity] = {}
        self.recommendations: dict[str, Recommendation] = {}

    def load_persisted_state(self) -> None:
        """Pull entities + recommendations from Postgres. Fast-path; see
        fusion.FusionEngine.load_persisted_state for the rationale on why
        observations are NOT preloaded."""
        loaded = store.load_entities_only(DOMAIN)
        self.entities.update(loaded.entities)
        self.recommendations.update(loaded.recommendations)

    # --- ingest -------------------------------------------------------
    def ingest(self, obs: Observation) -> Entity | None:
        """Route observation to the right fusion path."""
        self.observations[obs.obs_id] = obs
        store.put_observation(obs, domain=DOMAIN)
        audit_log.append(
            actor="system",
            event_type="observation_added",
            payload={"obs_id": obs.obs_id, "source": obs.source.value,
                     "domain": "wildfire"},
        )

        # Step 1: false-positive suppression — civilian platforms are
        # heavily biased AGAINST acting on a single thermal pixel.
        fp = self._is_known_fp(obs.geom)
        if fp is not None and obs.source in (SourceType.VIIRS, SourceType.GOES):
            return self._record_false_positive(obs, fp)

        if obs.source in (SourceType.VIIRS, SourceType.GOES):
            return self._ingest_thermal(obs)
        if obs.source == SourceType.OPTICAL:
            return self._ingest_smoke(obs)
        if obs.source == SourceType.WEATHER:
            return self._apply_weather_context(obs)
        if obs.source == SourceType.GROUND_CAM:
            return self._ingest_ground_camera(obs)
        return None

    # --- thermal -------------------------------------------------------
    def _ingest_thermal(self, obs: Observation) -> Entity:
        # Try to attach to an existing hotspot/fire_event cluster
        existing = self._nearest_active_fire(obs.geom, HOTSPOT_CLUSTER_KM,
                                              HOTSPOT_PERSISTENCE_WINDOW)
        if existing is not None:
            ent = self.entities[existing]
            ent.observation_ids.append(obs.obs_id)
            ent.last_seen = max(ent.last_seen, obs.t)
            ent.geom = obs.geom
            # Persistence raises confidence; cross-sensor agreement raises more
            ent.confidence = min(0.97, ent.confidence + 0.08)
            # If we now have multiple hotspot detections, promote to fire_event
            if (ent.type == EntityType.HOTSPOT and
                    len(ent.observation_ids) >= 2):
                ent.type = EntityType.FIRE_EVENT
                ent.priority_score = 0.85
                ent.notes = ("Persistent thermal anomaly across "
                             f"{len(ent.observation_ids)} detections.")
                audit_log.append(
                    actor="system",
                    event_type="entity_reclassified",
                    payload={"entity_id": ent.entity_id,
                             "to": "fire_event",
                             "reason": "thermal_persistence"},
                )
                self._make_recommendation(ent)
            else:
                audit_log.append(
                    actor="system",
                    event_type="observation_associated",
                    payload={"obs_id": obs.obs_id, "entity_id": existing,
                             "method": "thermal_cluster"},
                )
            store.put_entity(ent, domain=DOMAIN)
            return ent

        # Before creating a plain HOTSPOT, check whether this thermal
        # detection co-occurs with a recent CG lightning strike inside
        # a high-HDW cell. If so, this is the textbook dry-lightning
        # ignition signal — promote straight to LIGHTNING_IGNITION_RISK
        # so the operator's prepositioning happens BEFORE the fire
        # gets persistent enough to read as FIRE_EVENT.
        strike = _lightning_near(obs.geom.lon, obs.geom.lat, obs.t)
        hdw_risk = _hdw_risk_at(obs.geom.lon, obs.geom.lat)
        if strike is not None and hdw_risk >= LIGHTNING_FUSION_RISK_THRESHOLD:
            eid = f"fire_{uuid.uuid4().hex[:10]}"
            ent = Entity(
                entity_id=eid,
                type=EntityType.LIGHTNING_IGNITION_RISK,
                geom=obs.geom,
                last_seen=obs.t,
                first_seen=obs.t,
                confidence=0.72,
                priority_score=0.82,   # high — pre-emptive dispatch worthy
                observation_ids=[obs.obs_id],
                attrs={
                    "frp_mw":           obs.attrs.get("frp_mw"),
                    "scan_pixel_size_m": obs.attrs.get("pixel_size", 375),
                    "lightning_strike": strike,
                    "hdw_risk_score":   hdw_risk,
                },
                notes=(
                    f"Thermal detection co-located with CG strike "
                    f"({strike.get('polarity', '?')}, "
                    f"{strike.get('amplitude_ka', '?')} kA) at "
                    f"{strike.get('t', '?')}. HDW risk_score={hdw_risk:.2f}. "
                    f"Dry-lightning ignition signature — preposition "
                    f"before persistence."
                ),
            )
            self.entities[eid] = ent
            store.put_entity(ent, domain=DOMAIN)
            audit_log.append(
                actor="system",
                event_type="entity_created",
                payload={
                    "entity_id": eid,
                    "type": "lightning_ignition_risk",
                    "via": obs.source.value,
                    "strike_t": strike.get("t"),
                    "hdw_risk": hdw_risk,
                },
            )
            return ent

        # New hotspot — single detection, low priority until corroborated
        eid = f"fire_{uuid.uuid4().hex[:10]}"
        ent = Entity(
            entity_id=eid,
            type=EntityType.HOTSPOT,
            geom=obs.geom,
            last_seen=obs.t,
            first_seen=obs.t,
            confidence=0.55,                    # single pixel = low conf
            priority_score=0.30,                # watchlist, not action
            observation_ids=[obs.obs_id],
            attrs={"frp_mw": obs.attrs.get("frp_mw"),
                   "scan_pixel_size_m": obs.attrs.get("pixel_size", 375)},
            notes=("Single thermal detection. Awaiting persistence or "
                   "smoke corroboration before escalation."),
        )
        self.entities[eid] = ent
        store.put_entity(ent, domain=DOMAIN)
        audit_log.append(
            actor="system",
            event_type="entity_created",
            payload={"entity_id": eid, "type": "hotspot",
                     "via": obs.source.value},
        )
        return ent

    # --- smoke ---------------------------------------------------------
    def _ingest_smoke(self, obs: Observation) -> Entity:
        # Smoke detection corroborates nearby thermal anomalies
        nearest = self._nearest_active_fire(
            obs.geom, SMOKE_HOTSPOT_RADIUS_KM, SMOKE_HOTSPOT_WINDOW
        )
        if nearest is not None:
            ent = self.entities[nearest]
            ent.observation_ids.append(obs.obs_id)
            ent.last_seen = max(ent.last_seen, obs.t)
            ent.confidence = min(0.99, ent.confidence + 0.15)
            # Smoke + hotspot = fire_event (strong fusion)
            if ent.type == EntityType.HOTSPOT:
                ent.type = EntityType.FIRE_EVENT
                ent.priority_score = 0.90
                ent.notes = "Thermal anomaly corroborated by visible smoke plume."
                audit_log.append(
                    actor="system",
                    event_type="entity_reclassified",
                    payload={"entity_id": ent.entity_id,
                             "to": "fire_event",
                             "reason": "smoke_corroboration"},
                )
                self._make_recommendation(ent)
            else:
                audit_log.append(
                    actor="system",
                    event_type="observation_associated",
                    payload={"obs_id": obs.obs_id, "entity_id": nearest,
                             "method": "smoke_to_thermal"},
                )
            store.put_entity(ent, domain=DOMAIN)
            return ent

        # Orphan smoke detection — log as standalone smoke_plume entity
        eid = f"plume_{uuid.uuid4().hex[:10]}"
        ent = Entity(
            entity_id=eid,
            type=EntityType.SMOKE_PLUME,
            geom=obs.geom,
            last_seen=obs.t,
            first_seen=obs.t,
            confidence=0.60,
            priority_score=0.40,
            observation_ids=[obs.obs_id],
            notes=("Smoke plume detected without matching thermal anomaly. "
                   "Possibly distant fire, controlled burn, or dust."),
        )
        self.entities[eid] = ent
        store.put_entity(ent, domain=DOMAIN)
        audit_log.append(
            actor="system", event_type="entity_created",
            payload={"entity_id": eid, "type": "smoke_plume", "via": "optical"},
        )
        return ent

    def _ingest_ground_camera(self, obs: Observation) -> Entity:
        """Ground cameras corroborate within their FOV."""
        # For MVP we treat ground camera detections like smoke plumes
        # but with higher confidence (operator-validated)
        return self._ingest_smoke(obs)

    # --- weather (context, not entity-creating) -----------------------
    def _apply_weather_context(self, obs: Observation) -> None:
        """
        Weather observations attach to nearby fire_events as context,
        elevating priority when conditions are dangerous.
        """
        # Snapshot — wildfire ingest also mutates self.entities concurrently
        # if a separate ingest source ever lands. Defensive against future
        # threading bugs even if today only one thread mutates it.
        for ent in list(self.entities.values()):
            if ent.type != EntityType.FIRE_EVENT:
                continue
            if haversine_km(ent.geom, obs.geom) > 50:
                continue
            rh = obs.attrs.get("rh_pct", 100)
            wind = obs.attrs.get("wind_mph", 0)
            fuel = obs.attrs.get("fuel_moisture", 100)
            danger = (rh < 20) and (wind > 25) and (fuel < 8)
            ent.attrs.setdefault("weather", {}).update(obs.attrs)
            ent.observation_ids.append(obs.obs_id)
            if danger and ent.priority_score < 0.97:
                old = ent.priority_score
                ent.priority_score = min(0.99, ent.priority_score + 0.08)
                ent.notes += " RED FLAG conditions: low RH + high wind + critical fuels."
                audit_log.append(
                    actor="system",
                    event_type="priority_increased",
                    payload={"entity_id": ent.entity_id,
                             "from": round(old, 2),
                             "to": round(ent.priority_score, 2),
                             "reason": "red_flag_conditions"},
                )
            store.put_entity(ent, domain=DOMAIN)
        return None

    # --- false-positive suppression -----------------------------------
    def _is_known_fp(self, geom: Geom) -> str | None:
        for lon, lat, r, label in KNOWN_FP_SOURCES:
            if haversine_km(geom, Geom(lon=lon, lat=lat)) < r:
                return label
        return None

    def _record_false_positive(self, obs: Observation, label: str) -> Entity:
        eid = f"fp_{uuid.uuid4().hex[:8]}"
        ent = Entity(
            entity_id=eid,
            type=EntityType.FALSE_POSITIVE,
            geom=obs.geom,
            last_seen=obs.t, first_seen=obs.t,
            confidence=0.99,
            priority_score=0.0,
            observation_ids=[obs.obs_id],
            notes=f"Suppressed: known thermal source ({label}).",
            attrs={"suppression_reason": label},
        )
        self.entities[eid] = ent
        store.put_entity(ent, domain=DOMAIN)
        audit_log.append(
            actor="system", event_type="false_positive_suppressed",
            payload={"entity_id": eid, "label": label,
                     "obs_id": obs.obs_id},
        )
        return ent

    def _nearest_active_fire(self, geom: Geom, max_km: float,
                              max_window: timedelta) -> str | None:
        # TODO(phase 1): filter by max_window using observation timestamp.
        # The current call sites apply time filtering before calling this,
        # so the parameter is accepted for API stability but unused here.
        _ = max_window
        best, best_d = None, max_km + 1
        for eid, ent in list(self.entities.items()):
            if ent.type not in (EntityType.HOTSPOT, EntityType.FIRE_EVENT,
                                EntityType.SMOKE_PLUME):
                continue
            d = haversine_km(geom, ent.geom)
            if d < best_d:
                best, best_d = eid, d
        return best if best_d <= max_km else None

    # --- recommendations ----------------------------------------------
    def _make_recommendation(self, ent: Entity) -> Recommendation:
        wui = self._nearest_wui(ent.geom)
        if wui is not None:
            d_km, label = wui
            action = ActionType.EVACUATION_ADVISORY
            rationale = (f"Confirmed fire event {d_km:.1f} km from {label}. "
                         "Recommend evacuation advisory for affected zones. "
                         "REQUIRES named fire-officer approval before issuance.")
        elif ent.priority_score >= 0.85:
            action = ActionType.ALERT_FIRE_DISPATCH
            rationale = ("Confirmed fire event. Recommend dispatching ground "
                         "and aerial assets per local response plan.")
        else:
            action = ActionType.REQUEST_AERIAL_RECON
            rationale = ("Single-source thermal anomaly. Recommend manned or "
                         "drone aerial recon before committing surface assets.")

        rec = Recommendation(
            rec_id=f"rec_{uuid.uuid4().hex[:10]}",
            entity_id=ent.entity_id,
            action=action, rationale=rationale,
            evidence_obs_ids=list(ent.observation_ids),
            suggested_at=ent.last_seen,
        )
        self.recommendations[rec.rec_id] = rec
        store.put_recommendation(rec)
        audit_log.append(
            actor="system", event_type="recommendation_made",
            payload={"rec_id": rec.rec_id, "entity_id": ent.entity_id,
                     "action": action.value,
                     "evidence_count": len(rec.evidence_obs_ids)},
        )
        return rec

    def _nearest_wui(self, geom: Geom) -> tuple[float, str] | None:
        best = None
        for lon, lat, r, label in WUI_ZONES:
            d = haversine_km(geom, Geom(lon=lon, lat=lat))
            if d <= r:
                if best is None or d < best[0]:
                    best = (d, label)
        return best

    # --- decisions (same pattern as maritime) -------------------------
    def decide(self, entity_id: str, *, decision: Decision,
               operator: str, reason: str | None = None
               ) -> list[Recommendation]:
        affected = []
        now = datetime.utcnow()
        for rec in list(self.recommendations.values()):
            if rec.entity_id != entity_id or rec.decision != Decision.PENDING:
                continue
            rec.decision = decision
            rec.decided_by = operator
            rec.decided_at = now
            rec.decision_reason = reason
            store.put_recommendation(rec)
            audit_log.append(
                actor=operator, event_type="decision",
                payload={"rec_id": rec.rec_id, "entity_id": entity_id,
                         "decision": decision.value, "reason": reason or "",
                         "domain": "wildfire"},
            )
            affected.append(rec)
        return affected

    def lineage(self, entity_id: str) -> dict:
        ent = self.entities.get(entity_id)
        if ent is None:
            return {}
        obs = [self.observations[o] for o in ent.observation_ids
               if o in self.observations]
        related_audit = [a for a in audit_log.all()
                         if a.payload.get("entity_id") == entity_id
                         or a.payload.get("obs_id") in ent.observation_ids]
        related_recs = [r for r in list(self.recommendations.values())
                        if r.entity_id == entity_id]
        return {
            "entity": ent.model_dump(),
            "observations": [o.model_dump() for o in obs],
            "recommendations": [r.model_dump() for r in related_recs],
            "audit_chain": [a.model_dump() for a in related_audit],
        }


# Helper to bridge a missing API call — production would carry t with geom
def geom_t_helper(g):
    return datetime.utcnow()
