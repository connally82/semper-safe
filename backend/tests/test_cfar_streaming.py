"""
Regression tests for cfar.detect_vessels_in_stripes — the streaming
variant of CFAR added in May 2026 to keep peak memory bounded
independent of tile_px.

The contract is: streaming should produce the same clusters as the
full-tile path, even on synthetic targets planted right on stripe
boundaries (where naive stripe-handling would either miss them or
double-count them).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from cfar import (  # noqa: E402
    CfarConfig, detect_vessels, detect_vessels_in_stripes,
)


def _exp_clutter(rng: np.random.Generator, h: int, w: int) -> np.ndarray:
    """Synthetic exponential clutter — matches ocean radar return statistics
    well enough that CFAR's threshold formula gives the expected PFA."""
    return rng.exponential(scale=20.0, size=(h, w)).astype(np.float32)


def _plant_target(arr: np.ndarray, row: int, col: int, amp: float = 800.0) -> None:
    """Drop a 5×5 bright blob into `arr` centered at (row, col)."""
    arr[row - 2:row + 3, col - 2:col + 3] += amp


def _key(c) -> tuple[int, int]:
    return (round(c.centroid_row), round(c.centroid_col))


def test_streaming_matches_full_tile_for_random_targets() -> None:
    rng = np.random.default_rng(42)
    h, w = 800, 1200
    arr = _exp_clutter(rng, h, w)
    targets = [(100, 200), (300, 800), (500, 400), (700, 900), (250, 1100)]
    for r, c in targets:
        _plant_target(arr, r, c)

    cfg = CfarConfig(pfa=1e-6, min_cluster_pixels=4)
    full = detect_vessels(arr, cfg=cfg)
    stream = detect_vessels_in_stripes(
        lambda r0, r1: arr[r0:r1, :], h, w, cfg=cfg, stripe_height=200,
    )

    # Same number of clusters, same centroids.
    assert {_key(c) for c in full} == {_key(c) for c in stream}
    assert len(full) == len(stream) == len(targets)


def test_targets_on_stripe_boundaries_detected_exactly_once() -> None:
    """The trickiest case for any stripe-based detector: targets sitting
    right on the boundary between two stripes. Wrong implementations
    either miss them entirely or double-count them once per stripe."""
    rng = np.random.default_rng(99)
    h, w = 600, 600
    arr = _exp_clutter(rng, h, w)

    # Plant targets straddling the 200, 400 stripe boundaries.
    boundary_targets = [(199, 100), (200, 300), (201, 500),
                        (398, 300), (400, 100)]
    for r, c in boundary_targets:
        _plant_target(arr, r, c)

    cfg = CfarConfig(pfa=1e-6, min_cluster_pixels=4)
    full = detect_vessels(arr, cfg=cfg)
    stream = detect_vessels_in_stripes(
        lambda r0, r1: arr[r0:r1, :], h, w, cfg=cfg, stripe_height=200,
    )
    assert {_key(c) for c in full} == {_key(c) for c in stream}
    # No duplicates — exactly one cluster per planted target.
    assert len(stream) == len(boundary_targets)


def test_handles_empty_stripes_without_error() -> None:
    """Stripes with all-zero/over-land data should be skipped silently."""
    rng = np.random.default_rng(7)
    h, w = 400, 400
    arr = np.zeros((h, w), dtype=np.float32)
    # Add water in the bottom half only.
    arr[200:, :] = _exp_clutter(rng, 200, 400)
    _plant_target(arr, 350, 200)

    cfg = CfarConfig(pfa=1e-6, min_cluster_pixels=4)
    out = detect_vessels_in_stripes(
        lambda r0, r1: arr[r0:r1, :], h, w, cfg=cfg, stripe_height=100,
    )
    # The single planted target should be detected — and nothing in the
    # all-zero land stripes should produce false alarms.
    assert len(out) == 1
    assert _key(out[0]) == (350, 200)
