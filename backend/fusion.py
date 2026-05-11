"""
Fusion engine.

Three responsibilities:
  1. Detect AIS gaps (vessels that go dark)
  2. Correlate SAR detections to known AIS tracks (or flag as dark vessel)
  3. Maintain Entity records as observations stream in

The math here is intentionally simple. The point of the MVP is to prove the
plumbing — observation lineage, audit log, and the human-in-the-loop pattern.
Production swaps in proper Bayesian track fusion (IMM Kalman, JPDA) and a
learned association model. The interfaces don't change.
"""

from __future__ import annotations
import math
import uuid
from datetime import datetime, timedelta

from models import (
    Observation, Entity, EntityType, SourceType,
    Recommendation, ActionType, Decision, Geom,
)
from audit import audit_log
from db import store


DOMAIN = "maritime"


# ----------------------------------------------------------------------
# Tunables — would be config in production
# ----------------------------------------------------------------------
# Class-aware AIS gap thresholds. Different operating states have very
# different normal reporting cadences per ITU-R M.1371:
#   - Underway (nav=0/8): every 2-10 sec for Class A — 8 min of silence is anomalous
#   - At anchor / moored / aground (nav=1/5/6): every 3 min legally; in practice
#     moored vessels at dock can go 30+ min between reports without it meaning
#     anything. Tuned against live Texas-coast data 2026-05-08: 132 of 311
#     vessels were getting flagged at 8 min — 30% false-positive rate dominated
#     by moored vessels at the Houston/Galveston ports.
#   - Fishing (nav=7): often slow-reporting Class B; intermediate threshold.
#
# Rest is per-class; nav_status comes from the most recent PositionReport
# stored in entity.attrs.nav_status.
AIS_GAP_THRESHOLD = timedelta(minutes=8)              # underway / default
AIS_GAP_THRESHOLD_QUIESCENT = timedelta(minutes=60)   # at anchor, moored, aground
AIS_GAP_THRESHOLD_FISHING = timedelta(minutes=15)     # nav=7 fishing
QUIESCENT_NAV_STATES = {1, 5, 6}                      # 1=anchor, 5=moored, 6=aground
FISHING_NAV_STATES = {7}                              # 7=engaged in fishing

SAR_AIS_MATCH_RADIUS_KM = 1.5               # how close a SAR blob must be to an AIS report
SAR_AIS_MATCH_WINDOW = timedelta(minutes=20)

# Loitering detection thresholds. A cooperative AIS-reporting vessel that
# stays in essentially the same place for hours on end is anomalous in a
# way different from "AIS dropout" — it's NOT going dark, it's just not
# moving. Pattern matches narcotics handoffs, illegal fishing, smuggling
# rendezvous, and intentionally-stationary surveillance vessels.
#
# How we detect it: every observation refreshes ent.attrs['last_motion_at']
# whenever speed_kn exceeds LOITERING_SPEED_KN OR the position has moved
# more than LOITERING_DISTANCE_M since the entity's last reference point.
# detect_loitering() flips entities whose last_motion_at is older than
# LOITERING_THRESHOLD into type=LOITERING_VESSEL. A subsequent move resets
# them to VESSEL.
#
# Tuning notes:
#   - 0.5 kn floor is below the AIS minimum-resolvable speed (Class A
#     reports speed in 0.1 kn steps; Class B in 1 kn steps), so a true
#     drifter shows speed=0. Real underway traffic is >2 kn even at idle.
#   - 6 hours is long enough to exclude routine port loitering (line
#     handlers, pilot waits, customs) but short enough to catch operational
#     anomalies before they finish.
#   - 100 m moves: AIS position jitter is typically ~10 m at GPS-good
#     conditions and bumps to ~50 m near tall structures (Galveston piers)
#     so 100 m is a comfortable gate.
LOITERING_SPEED_KN = 0.5
LOITERING_DISTANCE_M = 100.0
LOITERING_THRESHOLD = timedelta(hours=6)

# AIS spoofing detection. Sanctioned tankers, illegal-fishing operators,
# and surveillance vessels routinely spoof AIS — falsifying position,
# MMSI, or ship_type. Three signals are easy to catch from the message
# stream alone:
#
#   1. **Implausible speed.** Position jumps that imply >SPOOF_MAX_SPEED_KN
#      between consecutive reports. The fastest commercial cargo cruises
#      at ~24 kn. Even a stratified-charged offshore patrol boat tops out
#      around 60 kn. We use 80 kn as a generous floor to ensure we're only
#      flagging clearly-bogus jumps and not GPS jitter or slow first-fix
#      acquisition. (Caveat: legitimate "AIS off, then on with stale
#      cached position" can also produce a teleport — combined with the
#      `dt > 0` and `dt < 1h` window this is rare.)
#
#   2. **Lat/lon at (0, 0).** ITU-R M.1371 defines (lat=91°, lon=181°)
#      as "not available", but malfunctioning transmitters and some
#      spoof tools default to literal (0, 0) — the so-called
#      "Null Island" cluster. Filtered upstream in aisstream.py for
#      Texas-AOI ingest, but we belt-and-suspenders here for any
#      out-of-band sources.
#
#   3. **MMSI of all zeros / non-ITU prefix.** ITU-R M.1371 reserves
#      MMSI prefixes by maritime ID country code. MMSI=0 or
#      111111111 / 999999999 are common spoofing tells.
#
# We start with (1) — the cleanest, most operationally interesting signal.
# (2) and (3) are a Phase 5 follow-up; the message ingestor would benefit
# from them more than the fusion engine.
SPOOF_MAX_SPEED_KN = 80.0
SPOOF_MIN_DT_S = 5.0       # below this, GPS reporting jitter dominates
SPOOF_MAX_DT_S = 3600.0    # above 1h, position changes mean nothing

# Convoy detection. Three or more cooperative vessels moving in
# formation — same heading, similar speed, tight spatial cluster — is a
# signal that's interesting on its own and amplified when paired with
# the other anomaly classes:
#   - escort flotillas (sanctioned-tanker conveys, paramilitary escorts)
#   - illegal-transshipment groups (mothership + fast skiffs)
#   - smuggling stacks (legitimate-looking cargo plus shadowing chase boat)
#   - or simply legitimate fleet ops worth labeling as such on the map
#
# Algorithm (cheap, runs on the gap-sweeper loop):
#   1. Filter to type=VESSEL with a recent speed > CONVOY_MIN_SPEED_KN
#      (stationary vessels aren't a convoy — they're a port queue).
#   2. Pairwise cluster by haversine ≤ CONVOY_RADIUS_KM AND heading
#      within ±CONVOY_HEADING_TOL_DEG AND speed within
#      ±CONVOY_SPEED_TOL_KN.
#   3. Connected-components: any group ≥ CONVOY_MIN_MEMBERS gets a
#      shared attrs.convoy_id (stable per-sweep, scoped to the group's
#      head MMSI). Vessels that drop out of formation lose the attr
#      on the next sweep.
#
# We don't introduce a new EntityType — convoy membership is a
# *contextual* attribute, not a classification: a convoy member is
# still a normal cooperative vessel. The frontend draws connecting
# segments between members of the same convoy_id; the underlying
# marker color stays the type-meta default.
CONVOY_RADIUS_KM = 2.0
CONVOY_HEADING_TOL_DEG = 15.0
CONVOY_SPEED_TOL_KN = 2.0
CONVOY_MIN_SPEED_KN = 3.0   # below this, "convoy" is just an anchorage
CONVOY_MIN_MEMBERS = 3


def gap_threshold_for(ent: Entity) -> timedelta:
    """Pick the AIS gap threshold appropriate for the entity's last-known
    navigational state. Fall back to the underway threshold for unknown
    states — that's the conservative choice (a vessel reporting nav=12
    'reserved' is more likely a software bug on an underway vessel than
    a genuinely-quiet docked one)."""
    nav = ent.attrs.get("nav_status")
    if nav in QUIESCENT_NAV_STATES:
        return AIS_GAP_THRESHOLD_QUIESCENT
    if nav in FISHING_NAV_STATES:
        return AIS_GAP_THRESHOLD_FISHING
    return AIS_GAP_THRESHOLD


# ----------------------------------------------------------------------
# Geo helpers
# ----------------------------------------------------------------------
def haversine_km(a: Geom, b: Geom) -> float:
    R = 6371.0
    la1, la2 = math.radians(a.lat), math.radians(b.lat)
    dla = math.radians(b.lat - a.lat)
    dlo = math.radians(b.lon - a.lon)
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
class FusionEngine:
    def __init__(self) -> None:
        self.observations: dict[str, Observation] = {}
        self.entities: dict[str, Entity] = {}
        self.recommendations: dict[str, Recommendation] = {}
        # AIS source_id (MMSI) → entity_id  (each MMSI is one vessel)
        self._mmsi_index: dict[str, str] = {}

    def load_persisted_state(self) -> None:
        """Pull entities + recommendations from Postgres into in-memory state.

        Uses the fast-path load_entities_only — observations are NOT
        preloaded. The full load_state's eager join across the
        entity↔observation many-to-many would walk 100k+ rows on a
        populated DB and time out the FastAPI startup handler. Engine
        doesn't actually need preloaded observations:
          - AIS ingest dedupes via _mmsi_index built from entities below
          - /track and /timeline endpoints already query the DB directly
          - SAR engine ingest is fed observations one at a time
        """
        loaded = store.load_entities_only(DOMAIN)
        self.entities.update(loaded.entities)
        self.recommendations.update(loaded.recommendations)
        # Rebuild MMSI index from loaded entities
        # Snapshot to avoid `dict changed size during iteration` when the
        # AISStream worker mutates self.entities concurrently from another
        # thread. The list() copy is atomic under the GIL.
        for ent in list(self.entities.values()):
            mmsi = ent.attrs.get("mmsi")
            if mmsi:
                self._mmsi_index[str(mmsi)] = ent.entity_id

    # --- ingest -------------------------------------------------------
    def ingest(self, obs: Observation) -> Entity:
        """Take an observation, fold it into the right entity."""
        self.observations[obs.obs_id] = obs
        store.put_observation(obs, domain=DOMAIN)
        audit_log.append(
            actor="system",
            event_type="observation_added",
            payload={"obs_id": obs.obs_id, "source": obs.source.value,
                     "source_id": obs.source_id},
        )

        if obs.source == SourceType.AIS:
            return self._ingest_ais(obs)
        if obs.source == SourceType.SAR:
            return self._ingest_sar(obs)
        # weather / others go to entity attrs in production; ignore for MVP
        raise ValueError(f"Unhandled source: {obs.source}")

    def _ingest_ais(self, obs: Observation) -> Entity:
        mmsi = obs.source_id
        eid = self._mmsi_index.get(mmsi)
        if eid is None:
            eid = f"ent_{uuid.uuid4().hex[:10]}"
            self._mmsi_index[mmsi] = eid
            ent = Entity(
                entity_id=eid,
                type=EntityType.VESSEL,
                geom=obs.geom,
                last_seen=obs.t,
                first_seen=obs.t,
                confidence=0.99,                # cooperative target
                priority_score=0.05,
                observation_ids=[obs.obs_id],
                # Seed last_motion_at to obs.t so brand-new entities don't
                # immediately get flagged as loitering — the timer starts now.
                attrs={"mmsi": mmsi,
                       "last_motion_at": obs.t.isoformat(),
                       "loitering_anchor": {"lon": obs.geom.lon,
                                            "lat": obs.geom.lat,
                                            "t": obs.t.isoformat()},
                       **obs.attrs},
            )
            self.entities[eid] = ent
            store.put_entity(ent, domain=DOMAIN)
            audit_log.append(
                actor="system",
                event_type="entity_created",
                payload={"entity_id": eid, "type": ent.type.value, "via": "ais"},
            )
            return ent

        ent = self.entities[eid]
        prev_geom = ent.geom
        prev_t = ent.last_seen

        # AIS spoof detection: compute implied speed from prev → new
        # position. We do this BEFORE updating ent.geom/last_seen so the
        # old reference point is still intact. A teleport doesn't reset
        # the entity to spoofed forever — only THIS message is rejected:
        # ent.geom stays at the prior position, ent.last_seen advances so
        # the gap sweeper doesn't also trigger, and we increment a
        # rolling spoof_count on attrs. After SPOOF_FLAG_COUNT spoofy
        # messages within the window, the entity gets re-typed.
        # (Single GPS glitches happen; persistent ones are a real attack.)
        spoofed_this_msg = False
        if prev_geom is not None and prev_t is not None:
            dt = (obs.t - prev_t).total_seconds()
            if SPOOF_MIN_DT_S < dt < SPOOF_MAX_DT_S:
                d_km = haversine_km(prev_geom, obs.geom)
                speed_kn = (d_km / dt) * 3600.0 / 1.852  # km/s → kn
                if speed_kn > SPOOF_MAX_SPEED_KN:
                    spoofed_this_msg = True
                    ent.attrs.setdefault("spoof_events", []).append({
                        "t": obs.t.isoformat(),
                        "implied_kn": round(speed_kn, 1),
                        "from": {"lon": prev_geom.lon, "lat": prev_geom.lat},
                        "to": {"lon": obs.geom.lon, "lat": obs.geom.lat},
                    })
                    # Trim the rolling window — keep only the last 10 events.
                    ent.attrs["spoof_events"] = ent.attrs["spoof_events"][-10:]
                    audit_log.append(
                        actor="system",
                        event_type="ais_spoof_suspected",
                        payload={"entity_id": eid, "mmsi": ent.attrs.get("mmsi"),
                                 "implied_kn": round(speed_kn, 1),
                                 "dt_s": round(dt, 1),
                                 "distance_km": round(d_km, 1)},
                    )

        if spoofed_this_msg:
            # Don't overwrite the trustworthy prior position with the
            # implausible one. Advance last_seen so the gap detector sees
            # we're still hearing from the MMSI; flip type if we've now
            # accumulated enough spoof events.
            ent.last_seen = obs.t
            n = len(ent.attrs.get("spoof_events", []))
            if n >= 2 and ent.type == EntityType.VESSEL:
                ent.type = EntityType.AIS_SPOOFED
                ent.priority_score = 0.7   # higher than loitering, lower than dark
                ent.notes = (f"AIS spoofing suspected — {n} implausible-speed "
                             f"reports in the rolling window.")
                audit_log.append(
                    actor="system",
                    event_type="entity_reclassified",
                    payload={"entity_id": eid, "to": "ais_spoofed",
                             "spoof_event_count": n},
                )
                self._make_recommendation(ent)
            store.put_entity(ent, domain=DOMAIN)
            return ent

        ent.geom = obs.geom
        ent.last_seen = obs.t
        ent.observation_ids.append(obs.obs_id)
        # Refresh telemetry-style attrs from the latest observation so that
        # gap_threshold_for(ent) sees the current navigational state. Don't
        # blow away the whole attrs dict — name/mmsi/destination from
        # ShipStaticData live there too.
        for telemetry_key in (
            "nav_status", "heading", "speed_kn", "true_heading", "rate_of_turn",
        ):
            if telemetry_key in obs.attrs:
                ent.attrs[telemetry_key] = obs.attrs[telemetry_key]
        # Loitering tracker: bump last_motion_at any time the vessel reports
        # real movement — either by speed-over-ground OR by absolute position
        # drift from the loitering anchor. Two signals because (a) GPS-stale
        # AIS messages sometimes report 0 kn while the actual position keeps
        # moving and (b) some Class B transceivers under-report SOG.
        moved = False
        speed = obs.attrs.get("speed_kn")
        if isinstance(speed, (int, float)) and speed >= LOITERING_SPEED_KN:
            moved = True
        anchor = ent.attrs.get("loitering_anchor")
        if anchor is not None:
            dist_km = haversine_km(
                Geom(lon=anchor["lon"], lat=anchor["lat"]),
                obs.geom,
            )
            if dist_km * 1000.0 >= LOITERING_DISTANCE_M:
                moved = True
        else:
            # Back-compat for entities that existed before this attr was added.
            if prev_geom is not None and haversine_km(prev_geom, obs.geom) * 1000.0 \
                    >= LOITERING_DISTANCE_M:
                moved = True
        if moved:
            ent.attrs["last_motion_at"] = obs.t.isoformat()
            ent.attrs["loitering_anchor"] = {
                "lon": obs.geom.lon, "lat": obs.geom.lat, "t": obs.t.isoformat(),
            }
            # Demotion: a moving vessel can't be loitering. Reset type if it
            # was previously flagged.
            if ent.type == EntityType.LOITERING_VESSEL:
                ent.type = EntityType.VESSEL
                ent.priority_score = 0.05
                audit_log.append(
                    actor="system",
                    event_type="entity_reclassified",
                    payload={"entity_id": eid, "to": "vessel",
                             "reason": "motion_resumed"},
                )
        # If a previously-dark vessel reappears on AIS, downgrade priority
        if ent.type == EntityType.AIS_GAP:
            ent.type = EntityType.VESSEL
            ent.priority_score = 0.05
            audit_log.append(
                actor="system",
                event_type="entity_reclassified",
                payload={"entity_id": eid, "to": "vessel", "reason": "ais_resumed"},
            )
        store.put_entity(ent, domain=DOMAIN)
        return ent

    def _ingest_sar(self, obs: Observation) -> Entity:
        # Try to match this SAR detection to a known vessel that was nearby
        # within the time window.
        candidate = self._best_ais_match(obs)
        if candidate is not None:
            ent = self.entities[candidate]
            ent.observation_ids.append(obs.obs_id)
            ent.last_seen = max(ent.last_seen, obs.t)
            ent.confidence = min(1.0, ent.confidence + 0.005)
            store.put_entity(ent, domain=DOMAIN)
            audit_log.append(
                actor="system",
                event_type="observation_associated",
                payload={"obs_id": obs.obs_id, "entity_id": candidate,
                         "method": "sar_to_ais_spatial_temporal"},
            )
            return ent

        # Also try to match to an existing dark vessel (track continuity
        # across consecutive SAR passes). Wider window since the vessel
        # may have moved between passes.
        candidate = self._best_dark_vessel_match(obs)
        if candidate is not None:
            ent = self.entities[candidate]
            ent.observation_ids.append(obs.obs_id)
            ent.geom = obs.geom
            ent.last_seen = obs.t
            # Repeated SAR detections with no AIS = stronger dark-vessel signal
            ent.confidence = min(0.95, ent.confidence + 0.05)
            ent.priority_score = min(1.0, ent.priority_score + 0.05)
            store.put_entity(ent, domain=DOMAIN)
            audit_log.append(
                actor="system",
                event_type="observation_associated",
                payload={"obs_id": obs.obs_id, "entity_id": candidate,
                         "method": "sar_track_continuity"},
            )
            return ent

        # No match → DARK VESSEL. New entity, high priority.
        eid = f"ent_{uuid.uuid4().hex[:10]}"
        ent = Entity(
            entity_id=eid,
            type=EntityType.DARK_VESSEL,
            geom=obs.geom,
            last_seen=obs.t,
            first_seen=obs.t,
            confidence=0.72,                  # SAR-only ID is uncertain
            priority_score=0.85,              # high — non-cooperative target
            observation_ids=[obs.obs_id],
            attrs=dict(obs.attrs),
            notes="SAR detection with no matching AIS report in window.",
        )
        self.entities[eid] = ent
        store.put_entity(ent, domain=DOMAIN)
        audit_log.append(
            actor="system",
            event_type="entity_created",
            payload={"entity_id": eid, "type": "dark_vessel", "via": "sar_unmatched"},
        )
        self._make_recommendation(ent)
        return ent

    def _best_ais_match(self, sar_obs: Observation) -> str | None:
        best_eid, best_score = None, 0.0
        for eid, ent in list(self.entities.items()):
            if ent.type not in (EntityType.VESSEL, EntityType.AIS_GAP):
                continue
            dt = abs((ent.last_seen - sar_obs.t).total_seconds())
            if dt > SAR_AIS_MATCH_WINDOW.total_seconds():
                continue
            d = haversine_km(ent.geom, sar_obs.geom)
            if d > SAR_AIS_MATCH_RADIUS_KM:
                continue
            score = (1 - d / SAR_AIS_MATCH_RADIUS_KM) * \
                    (1 - dt / SAR_AIS_MATCH_WINDOW.total_seconds())
            if score > best_score:
                best_score, best_eid = score, eid
        return best_eid

    def _best_dark_vessel_match(self, sar_obs: Observation) -> str | None:
        """Match a SAR detection to an existing dark-vessel track.

        Wider radius and longer window than AIS matching: a non-cooperative
        vessel between SAR passes may have moved several km, but should
        still be in the rough vicinity of its last detection.
        """
        TRACK_RADIUS_KM = 12.0
        TRACK_WINDOW_S = 90 * 60   # 90 minutes between passes is plausible
        best_eid, best_score = None, 0.0
        for eid, ent in list(self.entities.items()):
            if ent.type != EntityType.DARK_VESSEL:
                continue
            dt = abs((ent.last_seen - sar_obs.t).total_seconds())
            if dt == 0 or dt > TRACK_WINDOW_S:
                continue
            d = haversine_km(ent.geom, sar_obs.geom)
            if d > TRACK_RADIUS_KM:
                continue
            score = (1 - d / TRACK_RADIUS_KM) * (1 - dt / TRACK_WINDOW_S)
            if score > best_score:
                best_score, best_eid = score, eid
        return best_eid

    # --- gap detection -----------------------------------------------
    def detect_gaps(self, now: datetime) -> list[Entity]:
        """Sweep entities; mark vessels that have gone silent past their
        class-appropriate threshold (8min underway, 60min moored/anchored,
        15min fishing). See gap_threshold_for() for rationale."""
        flagged: list[Entity] = []
        # Snapshot to avoid `dict changed size during iteration` when the
        # AISStream worker mutates self.entities concurrently from another
        # thread. The list() copy is atomic under the GIL.
        for ent in list(self.entities.values()):
            if ent.type != EntityType.VESSEL:
                continue
            threshold = gap_threshold_for(ent)
            if (now - ent.last_seen) > threshold:
                ent.type = EntityType.AIS_GAP
                ent.priority_score = 0.55     # medium — could be benign or IUU
                ent.notes = (f"AIS dropout. Last report "
                             f"{ent.last_seen.isoformat()}.")
                store.put_entity(ent, domain=DOMAIN)
                audit_log.append(
                    actor="system",
                    event_type="entity_reclassified",
                    payload={"entity_id": ent.entity_id, "to": "ais_gap",
                             "last_seen": ent.last_seen.isoformat(),
                             "threshold_min": threshold.total_seconds() / 60,
                             "nav_status": ent.attrs.get("nav_status")},
                )
                self._make_recommendation(ent)
                flagged.append(ent)
        return flagged

    # --- loitering detection -----------------------------------------
    def detect_loitering(self, now: datetime) -> list[Entity]:
        """Sweep entities; flip cooperative VESSEL entities that haven't
        moved in LOITERING_THRESHOLD into LOITERING_VESSEL. The
        last_motion_at attr is bumped during ingest whenever the vessel
        reports speed >= LOITERING_SPEED_KN or its position drifts beyond
        LOITERING_DISTANCE_M from the anchor — see _ingest_ais.

        Skipped for AIS_GAP / DARK_VESSEL / non-vessel types — those have
        their own classifications and we don't want to clobber them with a
        weaker signal.
        """
        flagged: list[Entity] = []
        for ent in list(self.entities.values()):
            if ent.type != EntityType.VESSEL:
                continue
            last_motion_iso = ent.attrs.get("last_motion_at")
            if not last_motion_iso:
                continue
            try:
                last_motion = datetime.fromisoformat(last_motion_iso)
            except ValueError:
                continue
            stationary_for = now - last_motion
            if stationary_for < LOITERING_THRESHOLD:
                continue
            ent.type = EntityType.LOITERING_VESSEL
            # Higher priority than benign vessel, lower than dark — this
            # is a "watch closely" classification, not a "dispatch now" one.
            ent.priority_score = 0.6
            hours = round(stationary_for.total_seconds() / 3600.0, 1)
            ent.notes = (f"Stationary {hours}h. Last motion "
                         f"{last_motion.isoformat()}. Anchor "
                         f"({ent.geom.lon:.4f}, {ent.geom.lat:.4f}).")
            ent.attrs["loitering_hours"] = hours
            store.put_entity(ent, domain=DOMAIN)
            audit_log.append(
                actor="system",
                event_type="entity_reclassified",
                payload={"entity_id": ent.entity_id, "to": "loitering_vessel",
                         "stationary_hours": hours,
                         "last_motion_at": last_motion.isoformat()},
            )
            self._make_recommendation(ent)
            flagged.append(ent)
        return flagged

    # --- convoy detection --------------------------------------------
    def detect_convoys(self, now: datetime) -> list[list[Entity]]:
        """Sweep entities; cluster cooperative vessels moving in formation.

        Returns the list of convoy groups (each a list of Entity objects).
        Each member of a group ≥ CONVOY_MIN_MEMBERS gets ent.attrs['convoy_id']
        set to the group's head MMSI. Vessels that aren't in any group on
        this sweep lose the attr — convoy membership is ephemeral.

        Pairwise comparison is O(N²) but capped at N≈300 active vessels
        in the current AOI so it runs in <10 ms even on a shared-cpu
        Fly VM. If we ever scale past that, switch to a spatial index
        (grid bucketization on the lat/lon AOI).
        """
        candidates: list[Entity] = []
        for ent in list(self.entities.values()):
            if ent.type != EntityType.VESSEL:
                continue
            speed = ent.attrs.get("speed_kn")
            if not isinstance(speed, (int, float)) or speed < CONVOY_MIN_SPEED_KN:
                continue
            heading = ent.attrs.get("heading")
            if heading is None or not isinstance(heading, (int, float)):
                continue
            # 511 is the ITU-R M.1371 "not available" sentinel; treat as missing.
            if heading == 511:
                continue
            candidates.append(ent)

        # Union-find over pairs that satisfy all three thresholds.
        parent: dict[str, str] = {e.entity_id: e.entity_id for e in candidates}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        def heading_diff(a: float, b: float) -> float:
            d = abs(a - b) % 360.0
            return min(d, 360.0 - d)

        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                ai, aj = candidates[i], candidates[j]
                if abs(ai.attrs["speed_kn"] - aj.attrs["speed_kn"]) > CONVOY_SPEED_TOL_KN:
                    continue
                if heading_diff(ai.attrs["heading"], aj.attrs["heading"]) > CONVOY_HEADING_TOL_DEG:
                    continue
                if haversine_km(ai.geom, aj.geom) > CONVOY_RADIUS_KM:
                    continue
                union(ai.entity_id, aj.entity_id)

        # Bucket by root.
        groups: dict[str, list[Entity]] = {}
        for e in candidates:
            r = find(e.entity_id)
            groups.setdefault(r, []).append(e)

        out: list[list[Entity]] = []
        seen_eids: set[str] = set()
        for members in groups.values():
            if len(members) < CONVOY_MIN_MEMBERS:
                continue
            # Stable convoy_id per sweep: sort members by MMSI and use
            # the lowest as the convoy head. Lets the frontend re-render
            # the same connecting segments across consecutive ingests
            # even as the member set drifts by one or two vessels.
            members.sort(key=lambda m: m.attrs.get("mmsi") or m.entity_id)
            head = members[0]
            convoy_id = f"convoy_{(head.attrs.get('mmsi') or head.entity_id)[:10]}"
            for m in members:
                m.attrs["convoy_id"] = convoy_id
                m.attrs["convoy_size"] = len(members)
                seen_eids.add(m.entity_id)
                store.put_entity(m, domain=DOMAIN)
            out.append(members)
            audit_log.append(
                actor="system",
                event_type="convoy_detected",
                payload={"convoy_id": convoy_id, "n_members": len(members),
                         "member_mmsis": [m.attrs.get("mmsi") for m in members]},
            )

        # Demote: any previously-tagged vessel that's no longer in a
        # convoy this sweep loses the attr. (We don't have a fast index
        # of which entities had convoy_id last time, so just walk all
        # entities and check.)
        for ent in list(self.entities.values()):
            if ent.entity_id in seen_eids:
                continue
            if ent.attrs.get("convoy_id"):
                ent.attrs.pop("convoy_id", None)
                ent.attrs.pop("convoy_size", None)
                store.put_entity(ent, domain=DOMAIN)
        return out

    # --- recommendations ---------------------------------------------
    def _make_recommendation(self, ent: Entity) -> Recommendation:
        if ent.type == EntityType.DARK_VESSEL:
            action = ActionType.TASK_SAR_SAT
            rationale = ("SAR detection unmatched to any cooperative AIS target. "
                         "Recommend tasking next satellite pass for confirmation "
                         "before alerting surface assets.")
        elif ent.type == EntityType.AIS_GAP:
            action = ActionType.LOG_ONLY
            rationale = ("AIS dropout exceeds threshold. Watch for resumption "
                         "or correlate with next SAR pass. No surface dispatch "
                         "without corroboration.")
        elif ent.type == EntityType.LOITERING_VESSEL:
            action = ActionType.LOG_ONLY
            rationale = (
                "Cooperative AIS target stationary past loitering threshold. "
                "Pattern is consistent with operational pause (anchorage, "
                "pilot wait), illegal fishing, or transfer activity. "
                "Inspect vessel attrs (nav_status, ship_type, destination) "
                "before tasking surface assets."
            )
        elif ent.type == EntityType.AIS_SPOOFED:
            action = ActionType.TASK_SAR_SAT
            rationale = (
                "AIS reporting implausible-speed teleports (multiple events "
                "in rolling window). Position cannot be trusted. Recommend "
                "tasking next SAR pass to obtain a non-cooperative fix and "
                "alert Coast Guard if confirmed off the reported track."
            )

        rec = Recommendation(
            rec_id=f"rec_{uuid.uuid4().hex[:10]}",
            entity_id=ent.entity_id,
            action=action,
            rationale=rationale,
            evidence_obs_ids=list(ent.observation_ids),
            suggested_at=ent.last_seen,
        )
        self.recommendations[rec.rec_id] = rec
        store.put_recommendation(rec)
        audit_log.append(
            actor="system",
            event_type="recommendation_made",
            payload={"rec_id": rec.rec_id, "entity_id": ent.entity_id,
                     "action": action.value, "evidence_count": len(rec.evidence_obs_ids)},
        )
        return rec

    # --- decisions ----------------------------------------------------
    def decide(self, entity_id: str, *, decision: Decision,
               operator: str, reason: str | None = None) -> list[Recommendation]:
        """Operator approves/rejects all pending recs for an entity."""
        affected: list[Recommendation] = []
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
                actor=operator,
                event_type="decision",
                payload={"rec_id": rec.rec_id, "entity_id": entity_id,
                         "decision": decision.value, "reason": reason or ""},
            )
            affected.append(rec)
        return affected

    # --- queries ------------------------------------------------------
    def lineage(self, entity_id: str) -> dict:
        """Full provenance chain for an entity — every observation, every audit row."""
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
