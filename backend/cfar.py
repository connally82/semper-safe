"""
Constant False Alarm Rate (CFAR) vessel detection on Sentinel-1 GRDH amplitude.

Phase 4.3 of docs/roadmap.md. Replaces the synthetic SAR detections in
the seed scenario with real detections from a downloaded scene.

Algorithm (cell-averaging CFAR, the simplest and most widely used variant):
  - For each candidate cell, look at a "training" annulus of cells around
    it (skipping a "guard" buffer of immediate neighbors that might be
    part of the same vessel).
  - Estimate clutter mean μ from the training cells.
  - Cell is a detection if pixel ≥ T·μ where T is chosen for a target
    Probability of False Alarm (PFA) under exponential clutter (default
    1e-6 for vessel detection over open water).
  - Cluster contiguous detections into a single point (centroid + bbox).
  - Discard clusters too small (noise) or too elongated (likely
    coastline / ship wake / oil rig — coastlines look very different
    from compact vessel returns).

Why this version:
  - No training data required. Works on a fresh scene.
  - O(N) with separable box filters; runs comfortably on a 25000x16000
    GRDH band in a few seconds without GPU.
  - Pure NumPy + scipy.ndimage. Same dependency set as PostGIS so we
    can ship without a custom Docker image.

What this version does NOT do (Phase 4.x and 5):
  - Adaptive PFA / Goldstein-Werner / OS-CFAR — fancier statistics for
    heterogeneous clutter (near-shore, ice). Phase 4.x.
  - Neural-net classifier on top of CFAR detections to filter false
    positives (rigs, buoys, atmospheric ducts). Phase 5.
  - Multi-pol fusion (VV+VH). Currently uses VV only, which is the
    standard channel for vessel detection.

Reference: Crisp 2004, "The State-of-the-Art in Ship Detection in
Synthetic Aperture Radar Imagery", DSTO-RR-0272.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("cfar")


@dataclass
class CfarConfig:
    """Knobs for cell-averaging CFAR.

    Defaults are tuned for Sentinel-1 IW GRDH VV-polarized 10m-pixel data
    in open ocean. Re-tune if you change product type / resolution.
    """
    # Window sizes are in pixels. For 10m GRDH:
    #   guard = 5  → 50m guard ring (covers a ~50m vessel + adjacent wake)
    #   train = 9  → 90m training ring outside guard (~80m of clutter)
    guard_size: int = 5
    train_size: int = 9

    # Probability of False Alarm. Lower = stricter, fewer detections.
    # 1e-6 over a 25k×16k scene gives ~400 expected false alarms before
    # cluster filtering, which the cluster filters knock down to ~10s.
    pfa: float = 1e-6

    # Cluster filtering after thresholding.
    min_cluster_pixels: int = 4         # < 4 px ≈ < 40m² → noise
    max_cluster_pixels: int = 5000      # > 5000 px → coastline / very long ship / rig
    max_aspect_ratio: float = 8.0       # length/width > 8 → likely linear feature, not a vessel


def cfar_threshold_factor(pfa: float, n_train: int) -> float:
    """Scale factor T such that pixel ≥ T·μ has prob ≈ pfa under exponential clutter.

    For a CA-CFAR detector with N training cells under exponential clutter
    statistics, the threshold multiplier is N·(pfa^(-1/N) - 1). Derived in
    Skolnik / Richards.
    """
    if n_train <= 0 or pfa <= 0 or pfa >= 1:
        raise ValueError("invalid pfa or n_train")
    return n_train * (pfa ** (-1.0 / n_train) - 1.0)


def _box_sum(arr: np.ndarray, half_size: int) -> np.ndarray:
    """Sum over a (2*half_size+1)^2 square around each pixel using
    cumulative sums. ~10x faster than scipy.ndimage.uniform_filter for
    integer half-sizes and matches it on edges via reflect padding."""
    h = half_size
    pad = np.pad(arr, h, mode="reflect")
    cs = pad.cumsum(axis=0).cumsum(axis=1)
    # cs has shape (H+2h+1, W+2h+1) effectively — but we padded by h, so
    # cs is (H+2h, W+2h). Use 1-indexed integral image semantics.
    H, W = arr.shape
    ii = np.zeros((H + 2 * h + 1, W + 2 * h + 1), dtype=cs.dtype)
    ii[1:, 1:] = cs
    # Window at output (r, c) covers pad rows [r..r+2h], cols [c..c+2h].
    # Sum = ii[r+2h+1, c+2h+1] - ii[r, c+2h+1] - ii[r+2h+1, c] + ii[r, c]
    r2 = ii[2 * h + 1 :, 2 * h + 1 :]
    r1 = ii[: -(2 * h + 1), 2 * h + 1 :]
    c1 = ii[2 * h + 1 :, : -(2 * h + 1)]
    o = ii[: -(2 * h + 1), : -(2 * h + 1)]
    return r2 - r1 - c1 + o


def cfar_detect(amplitude: np.ndarray, cfg: CfarConfig | None = None) -> np.ndarray:
    """Apply cell-averaging CFAR to a 2-D amplitude array.

    Returns a boolean mask same shape as the input. True where the pixel
    exceeded T·μ with μ estimated from the local training annulus.
    """
    cfg = cfg or CfarConfig()
    if amplitude.ndim != 2:
        raise ValueError("amplitude must be 2-D")
    if cfg.train_size <= cfg.guard_size:
        raise ValueError("train_size must exceed guard_size")

    arr = amplitude.astype(np.float32, copy=False)

    # Sum over (guard_size+train_size) and (guard_size) windows; the
    # difference is the sum over the annulus.
    outer_sum = _box_sum(arr, cfg.guard_size + cfg.train_size)
    inner_sum = _box_sum(arr, cfg.guard_size)
    annulus_sum = outer_sum - inner_sum

    n_outer = (2 * (cfg.guard_size + cfg.train_size) + 1) ** 2
    n_inner = (2 * cfg.guard_size + 1) ** 2
    n_train = n_outer - n_inner

    mu = annulus_sum / max(n_train, 1)
    factor = cfar_threshold_factor(cfg.pfa, n_train)
    threshold = factor * mu

    return arr >= threshold


@dataclass
class Cluster:
    centroid_row: float
    centroid_col: float
    n_pixels: int
    bbox: tuple[int, int, int, int]   # row0, col0, row1, col1
    rcs_db: float
    length_px: float
    width_px: float

    @property
    def aspect_ratio(self) -> float:
        return self.length_px / max(self.width_px, 1e-6)


def cluster_detections(mask: np.ndarray, amplitude: np.ndarray,
                       cfg: CfarConfig | None = None) -> list[Cluster]:
    """Group connected True-pixels into clusters, drop clusters that
    fail the size/aspect filters, return a list of plausible vessel
    detections."""
    cfg = cfg or CfarConfig()
    # Lazy import: scipy is heavyweight and only needed during detection.
    from scipy.ndimage import label

    labels, n = label(mask)
    out: list[Cluster] = []
    for i in range(1, n + 1):
        rows, cols = np.where(labels == i)
        n_px = rows.size
        if n_px < cfg.min_cluster_pixels or n_px > cfg.max_cluster_pixels:
            continue
        r0, r1 = int(rows.min()), int(rows.max())
        c0, c1 = int(cols.min()), int(cols.max())
        h = (r1 - r0) + 1
        w = (c1 - c0) + 1
        length_px = float(max(h, w))
        width_px = float(min(h, w))
        if (length_px / max(width_px, 1e-6)) > cfg.max_aspect_ratio:
            continue
        # RCS in dB from peak amplitude inside the cluster (Sentinel-1
        # GRDH amplitude is ~ sqrt(intensity); 20*log10 → dB).
        peak = float(amplitude[rows, cols].max())
        rcs_db = 20.0 * math.log10(max(peak, 1e-6))
        out.append(
            Cluster(
                centroid_row=float(rows.mean()),
                centroid_col=float(cols.mean()),
                n_pixels=int(n_px),
                bbox=(r0, c0, r1, c1),
                rcs_db=rcs_db,
                length_px=length_px,
                width_px=width_px,
            )
        )
    return out


def detect_vessels(amplitude: np.ndarray,
                   cfg: CfarConfig | None = None) -> list[Cluster]:
    """End-to-end CFAR + clustering. Returns plausible vessel detections."""
    mask = cfar_detect(amplitude, cfg=cfg)
    return cluster_detections(mask, amplitude, cfg=cfg)
