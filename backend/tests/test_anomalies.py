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
