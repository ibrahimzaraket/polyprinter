"""Sample-size adjustment (PRD §6.2, Audit F4).

A 300% ROI over 6 trades must not outrank 22% over 400 — the leaderboard
selects on realised profit, so raw ROI on a small sample is mostly luck.
Simple Bayesian shrinkage toward the population mean, k≈30.
"""

from __future__ import annotations


def shrink(n: int, roi: float, pop_mean: float, k: float = 30.0) -> float:
    """adj = (n*roi + k*pop_mean) / (n + k)"""
    if n < 0:
        raise ValueError("n must be >= 0")
    return (n * roi + k * pop_mean) / (n + k)


def population_mean(rois: list[float]) -> float:
    """Mean ROI across the current candidate pool. Recomputed per scan wave
    (not a fixed constant) so shrinkage tracks the population it's actually
    being compared against.
    """
    if not rois:
        return 0.0
    return sum(rois) / len(rois)
