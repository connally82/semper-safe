"""
Unit tests for the maritime anomaly classifiers added in May 2026:
  - LOITERING_VESSEL: AIS-cooperative vessel stationary > N hours
  - AIS_SPOOFED:     AIS-cooperative vessel reporting impossible-speed jumps

These exercise FusionEngine in isolation (no FastAPI, no Postgres). The
engine writes through to `store` and `audit_log`, but those default to
in-memory implementations when DATABASE_URL isn't set — which is the
case in CI.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Same sys.path dance as test_smoke.py — sibling-module imports.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402

from fusion import FusionEngine, LOITERING_THRESHOLD  # noqa: E402
from models import EntityType, Geom, Observation, SourceType  # noqa: E402


T0 = datetime(2026, 5, 9, 10, 0, 0, tzinfo=timezone.utc)


def _obs(oid: str, mmsi: str, t: datetime, lon: float, lat: float,
         speed_kn: float = 0.0) -> Observation:
    """Tiny helper — every test below builds AIS observations the same way."""
    return Observation(
        obs_id=oid, source=SourceType.AIS, source_id=mmsi,
        t=t, geom=Geom(lon=lon, lat=lat),
        # h3_cell is required by the model schema; the value doesn't matter
        # for the anomaly tests — we use a placeholder that's the right shape.
        h3_cell="8800000000fffff",
        attrs={"speed_kn": speed_kn},
    )


# ---------------------------------------------------------------------------
# LOITERING_VESSEL detection
# ---------------------------------------------------------------------------


class TestLoiteringDetection:
    def test_stationary_vessel_flagged(self) -> None:
        """8h of zero-speed reports at the same point → LOITERING_VESSEL."""
        eng = FusionEngine()
        for i in range(48):  # 48 × 10min = 8h
            eng.ingest(_obs(f"a{i}", "111", T0 + timedelta(minutes=10 * i),
                            -95.0, 29.0, speed_kn=0.0))

        flagged = eng.detect_loitering(T0 + timedelta(hours=8, minutes=1))
        assert len(flagged) == 1
        assert flagged[0].attrs["mmsi"] == "111"
        assert flagged[0].type == EntityType.LOITERING_VESSEL
        assert flagged[0].attrs["loitering_hours"] >= 8.0

    def test_moving_vessel_not_flagged(self) -> None:
        """Cruising 8 kn over 8h → never flagged, even at sweep time."""
        eng = FusionEngine()
        for i in range(48):
            eng.ingest(_obs(f"b{i}", "222", T0 + timedelta(minutes=10 * i),
                            -95.0 + 0.01 * i, 29.0, speed_kn=8.0))

        flagged = eng.detect_loitering(T0 + timedelta(hours=8, minutes=1))
        assert flagged == []
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "222")
        assert ent.type == EntityType.VESSEL

    def test_briefly_stationary_then_moves_not_flagged(self) -> None:
        """4h stationary, then 4h moving — last_motion_at is fresh, not flagged."""
        eng = FusionEngine()
        for i in range(24):  # 4h still
            eng.ingest(_obs(f"c1_{i}", "333", T0 + timedelta(minutes=10 * i),
                            -94.5, 28.5, speed_kn=0.0))
        for i in range(24, 48):  # 4h moving
            eng.ingest(_obs(f"c2_{i}", "333", T0 + timedelta(minutes=10 * i),
                            -94.5 + 0.005 * (i - 24), 28.5, speed_kn=5.0))

        flagged = eng.detect_loitering(T0 + timedelta(hours=8, minutes=1))
        assert flagged == []

    def test_demotion_when_motion_resumes(self) -> None:
        """Once flagged, a single motion observation puts the vessel back."""
        eng = FusionEngine()
        for i in range(48):
            eng.ingest(_obs(f"d{i}", "444", T0 + timedelta(minutes=10 * i),
                            -95.0, 29.0, speed_kn=0.0))
        sweep = T0 + timedelta(hours=8, minutes=1)
        eng.detect_loitering(sweep)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "444")
        assert ent.type == EntityType.LOITERING_VESSEL

        # Now report a motion-and-position-change observation.
        eng.ingest(_obs("d_move", "444", sweep + timedelta(minutes=5),
                        -94.95, 29.0, speed_kn=6.0))
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "444")
        assert ent.type == EntityType.VESSEL


# ---------------------------------------------------------------------------
# AIS_SPOOFED detection
# ---------------------------------------------------------------------------


class TestAisSpoofDetection:
    def test_legitimate_cruise_not_flagged(self) -> None:
        """A normal vessel (small position deltas, plausible speed) is fine."""
        eng = FusionEngine()
        for i in range(5):
            eng.ingest(_obs(f"a{i}", "111", T0 + timedelta(seconds=30 * i),
                            -95.0 + 0.001 * i, 29.0, speed_kn=8.0))
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "111")
        assert ent.type == EntityType.VESSEL
        assert ent.attrs.get("spoof_events", []) == []

    def test_repeated_teleports_flag_as_spoofed(self) -> None:
        """Two ~3000-kn-implied jumps in a row → AIS_SPOOFED."""
        eng = FusionEngine()
        eng.ingest(_obs("b0", "222", T0,                                    -95.0, 29.0))
        eng.ingest(_obs("b1", "222", T0 + timedelta(seconds=60),           -94.0, 29.0))
        eng.ingest(_obs("b2", "222", T0 + timedelta(seconds=120),          -93.5, 29.0))

        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "222")
        assert ent.type == EntityType.AIS_SPOOFED
        assert len(ent.attrs["spoof_events"]) >= 2
        # Implied speeds should be obviously bogus (1000s of knots).
        assert all(ev["implied_kn"] > 1000.0 for ev in ent.attrs["spoof_events"])

    def test_single_glitch_does_not_flag(self) -> None:
        """One isolated impossible jump is treated as a GPS glitch, not a flip."""
        eng = FusionEngine()
        eng.ingest(_obs("c0", "333", T0,                                    -94.0, 28.0))
        eng.ingest(_obs("c1", "333", T0 + timedelta(seconds=60),           -90.0, 28.0))

        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "333")
        assert ent.type == EntityType.VESSEL  # type still VESSEL after one event
        assert len(ent.attrs.get("spoof_events", [])) == 1

    def test_spoof_does_not_overwrite_trustworthy_position(self) -> None:
        """When a spoof event fires, ent.geom must NOT be updated to the
        bogus position — the prior fix is the one we trust."""
        eng = FusionEngine()
        eng.ingest(_obs("d0", "444", T0,                                    -95.0, 29.0))
        eng.ingest(_obs("d1", "444", T0 + timedelta(seconds=60),           -94.0, 29.0))

        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "444")
        # Position is still the FIRST report, not the teleport endpoint.
        assert ent.geom.lon == pytest.approx(-95.0, abs=1e-9)
        assert ent.geom.lat == pytest.approx(29.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Convoy detection
# ---------------------------------------------------------------------------


def _heading_obs(oid: str, mmsi: str, t: datetime, lon: float, lat: float,
                 speed_kn: float, heading: float) -> Observation:
    """Same as _obs but with explicit speed + heading attrs (convoy det needs them)."""
    return Observation(
        obs_id=oid, source=SourceType.AIS, source_id=mmsi,
        t=t, geom=Geom(lon=lon, lat=lat),
        h3_cell="8800000000fffff",
        attrs={"speed_kn": speed_kn, "heading": heading},
    )


class TestConvoyDetection:
    def test_four_vessel_formation_flagged(self) -> None:
        """4 vessels, eastbound at 10 kn, ~1 km spacing → one convoy."""
        eng = FusionEngine()
        for i, mmsi in enumerate(["111", "112", "113", "114"]):
            eng.ingest(_heading_obs(f"a{i}", mmsi, T0,
                                    -95.0 + i * 0.012, 29.0,
                                    speed_kn=10.0 + i * 0.3, heading=90 + i * 2))
        convoys = eng.detect_convoys(T0)
        assert len(convoys) == 1
        assert len(convoys[0]) == 4
        cids = {m.attrs.get("convoy_id") for m in convoys[0]}
        assert len(cids) == 1 and next(iter(cids)).startswith("convoy_")

    def test_solo_vessel_not_in_convoy(self) -> None:
        """A south-bound vessel in the same area but not in formation
        is not assigned a convoy_id."""
        eng = FusionEngine()
        # Convoy
        for i, mmsi in enumerate(["111", "112", "113"]):
            eng.ingest(_heading_obs(f"a{i}", mmsi, T0,
                                    -95.0 + i * 0.01, 29.0,
                                    speed_kn=10.0, heading=90))
        # Loner — same area, perpendicular heading
        eng.ingest(_heading_obs("b0", "999", T0, -94.99, 29.01,
                                speed_kn=10.0, heading=180))
        eng.detect_convoys(T0)
        b = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "999")
        assert "convoy_id" not in b.attrs

    def test_two_vessel_pair_below_min_members(self) -> None:
        """A 2-vessel pair, even in formation, does not qualify as a convoy."""
        eng = FusionEngine()
        for i, mmsi in enumerate(["201", "202"]):
            eng.ingest(_heading_obs(f"c{i}", mmsi, T0,
                                    -93.0 + i * 0.005, 28.0,
                                    speed_kn=8.0, heading=45))
        eng.detect_convoys(T0)
        for mmsi in ("201", "202"):
            ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == mmsi)
            assert "convoy_id" not in ent.attrs

    def test_stationary_vessels_skipped(self) -> None:
        """Vessels below CONVOY_MIN_SPEED_KN aren't candidates — anchorages
        aren't convoys."""
        eng = FusionEngine()
        for i, mmsi in enumerate(["301", "302", "303"]):
            eng.ingest(_heading_obs(f"d{i}", mmsi, T0,
                                    -96.0 + i * 0.005, 27.5,
                                    speed_kn=0.5, heading=0))
        convoys = eng.detect_convoys(T0)
        assert convoys == []

    def test_membership_demoted_when_vessel_leaves(self) -> None:
        """After a vessel breaks formation, the next sweep removes its convoy_id."""
        eng = FusionEngine()
        # Form a convoy
        for i, mmsi in enumerate(["111", "112", "113"]):
            eng.ingest(_heading_obs(f"a{i}", mmsi, T0,
                                    -95.0 + i * 0.01, 29.0,
                                    speed_kn=10.0, heading=90))
        eng.detect_convoys(T0)
        assert all("convoy_id" in eng.entities[eid].attrs
                   for eid in eng.entities)

        # Vessel 113 makes a sharp turn — heading 270 now (westbound)
        eng.ingest(_heading_obs("a3_turn", "113",
                                T0 + timedelta(minutes=5),
                                -94.95, 29.0,
                                speed_kn=10.0, heading=270))
        # 111 and 112 keep going east; only 2 left in formation → no convoy
        eng.detect_convoys(T0 + timedelta(minutes=5))
        for mmsi in ("111", "112", "113"):
            ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == mmsi)
            assert "convoy_id" not in ent.attrs, f"{mmsi} should be demoted"


# ---------------------------------------------------------------------------
# Port-skipping detection
# ---------------------------------------------------------------------------


def _route_obs(oid: str, mmsi: str, t: datetime, lon: float, lat: float,
               speed_kn: float, heading: float, dest: str) -> Observation:
    """Observation with destination + heading attrs — port-skipping needs both."""
    return Observation(
        obs_id=oid, source=SourceType.AIS, source_id=mmsi,
        t=t, geom=Geom(lon=lon, lat=lat),
        h3_cell="8800000000fffff",
        attrs={"speed_kn": speed_kn, "heading": heading, "destination": dest},
    )


class TestPortSkippingDetection:
    def test_off_course_for_declared_destination_flagged(self) -> None:
        """Vessel declares HOUSTON (NNW of position) but heads east →
        flagged as port_skipping, attrs.port_skip populated with details."""
        eng = FusionEngine()
        eng.ingest(_route_obs("a0", "111", T0, -94.5, 28.5,
                              speed_kn=10, heading=90, dest="HOUSTON"))
        flagged = eng.detect_port_skipping(T0)
        assert len(flagged) == 1
        ent = flagged[0]
        assert ent.type == EntityType.PORT_SKIPPING
        ps = ent.attrs.get("port_skip")
        assert ps and ps["declared_port"] == "Houston"
        assert ps["heading_diff_deg"] > 60.0

    def test_on_course_not_flagged(self) -> None:
        """Same position + destination, but heading toward Houston (~330°)
        → no flag. Cooperative vessel doing what AIS says."""
        eng = FusionEngine()
        eng.ingest(_route_obs("b0", "222", T0, -94.5, 28.5,
                              speed_kn=10, heading=330, dest="HOUSTON"))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "222")
        assert ent.type == EntityType.VESSEL

    def test_no_destination_not_flagged(self) -> None:
        """Vessel reports no destination → can't be port-skipping."""
        eng = FusionEngine()
        eng.ingest(_route_obs("c0", "333", T0, -94.5, 28.5,
                              speed_kn=10, heading=90, dest=""))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "333")
        assert ent.type == EntityType.VESSEL

    def test_unknown_destination_not_flagged(self) -> None:
        """Destination doesn't match any Gulf port → no flag."""
        eng = FusionEngine()
        eng.ingest(_route_obs("d0", "444", T0, -94.5, 28.5,
                              speed_kn=10, heading=90, dest="LIMA"))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "444")
        assert ent.type == EntityType.VESSEL

    def test_too_close_to_port_not_flagged(self) -> None:
        """Vessel inside the inner ring (port approach) — heading
        divergence is normal pilot maneuvering, not a routing anomaly."""
        eng = FusionEngine()
        # 5 km north of Houston port-mouth — well inside the 8 km inner ring.
        eng.ingest(_route_obs("e0", "555", T0, -95.01, 29.77,
                              speed_kn=8, heading=90, dest="HOUSTON"))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "555")
        assert ent.type == EntityType.VESSEL

    def test_low_speed_not_flagged(self) -> None:
        """Vessel speed < threshold (drifting, anchored, pilot wait) → no flag."""
        eng = FusionEngine()
        eng.ingest(_route_obs("f0", "666", T0, -94.5, 28.5,
                              speed_kn=2, heading=90, dest="HOUSTON"))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "666")
        assert ent.type == EntityType.VESSEL

    def test_demotion_after_course_correction(self) -> None:
        """Vessel flagged as port_skipping, then turns toward port → demoted."""
        eng = FusionEngine()
        eng.ingest(_route_obs("g0", "777", T0, -94.5, 28.5,
                              speed_kn=10, heading=90, dest="HOUSTON"))
        eng.detect_port_skipping(T0)
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "777")
        assert ent.type == EntityType.PORT_SKIPPING

        # Course correction toward Houston (~330°)
        eng.ingest(_route_obs("g1", "777", T0 + timedelta(minutes=10),
                              -94.5, 28.5,
                              speed_kn=10, heading=330, dest="HOUSTON"))
        eng.detect_port_skipping(T0 + timedelta(minutes=10))
        ent = next(e for e in eng.entities.values() if e.attrs.get("mmsi") == "777")
        assert ent.type == EntityType.VESSEL
        assert "port_skip" not in ent.attrs
