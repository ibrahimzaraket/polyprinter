import pytest

from polyprinter.scout.shrinkage import population_mean, shrink


def test_shrink_pulls_small_sample_toward_population_mean():
    # A merely-good ROI over a big sample should outrank a flashy ROI over
    # a handful of trades — the exact failure mode Audit F4 names (PRD §6.2).
    small_sample = shrink(n=6, roi=0.50, pop_mean=0.10, k=30)
    large_sample = shrink(n=400, roi=0.22, pop_mean=0.10, k=30)
    assert small_sample < large_sample

    # And shrinkage moves the small sample much closer to the population
    # mean than it moves the large sample from its own raw ROI.
    assert abs(small_sample - 0.10) < abs(0.50 - 0.10)
    assert abs(large_sample - 0.22) < abs(small_sample - 0.50)


def test_shrink_with_zero_trades_equals_population_mean():
    assert shrink(n=0, roi=5.0, pop_mean=0.15, k=30) == 0.15


def test_shrink_with_huge_sample_approaches_raw_roi():
    result = shrink(n=100_000, roi=0.5, pop_mean=0.0, k=30)
    assert abs(result - 0.5) < 0.001


def test_population_mean_empty():
    assert population_mean([]) == 0.0


def test_population_mean_basic():
    assert population_mean([0.1, 0.2, 0.3]) == pytest.approx(0.2)
