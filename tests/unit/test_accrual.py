"""The arithmetic behind deciding a class of reports is safe to release unreviewed."""

from __future__ import annotations

import math

import pytest

from radreport.autonomy.accrual import POSTERIOR_THRESHOLD, _beta_cdf, posterior_non_inferiority


@pytest.mark.parametrize("x", [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99])
def test_beta_1_1_is_the_uniform_cdf(x: float) -> None:
    """Beta(1,1) is uniform, so its CDF is the identity."""
    assert _beta_cdf(x, 1.0, 1.0) == pytest.approx(x, abs=1e-9)


@pytest.mark.parametrize("x", [0.2, 0.5, 0.8])
def test_beta_2_2_matches_its_closed_form(x: float) -> None:
    """Beta(2,2): F(x) = 3x² − 2x³."""
    assert _beta_cdf(x, 2.0, 2.0) == pytest.approx(3 * x**2 - 2 * x**3, abs=1e-9)


@pytest.mark.parametrize("x", [0.1, 0.3, 0.7, 0.95])
def test_beta_half_half_matches_the_arcsine_law(x: float) -> None:
    """Beta(½,½) is the arcsine distribution: F(x) = (2/π)·arcsin(√x)."""
    expected = (2 / math.pi) * math.asin(math.sqrt(x))
    assert _beta_cdf(x, 0.5, 0.5) == pytest.approx(expected, abs=1e-9)


def test_the_cdf_is_bounded_and_monotone() -> None:
    assert _beta_cdf(0.0, 3.0, 5.0) == 0.0
    assert _beta_cdf(1.0, 3.0, 5.0) == 1.0
    values = [_beta_cdf(x / 20, 3.0, 5.0) for x in range(21)]
    assert values == sorted(values)


def test_the_symmetry_identity_holds_across_the_branch_switch() -> None:
    """I_x(a,b) = 1 − I_{1−x}(b,a). The implementation switches branches partway through, and a bug there shows up only as an asymmetry."""
    for x, a, b in [(0.2, 2.0, 5.0), (0.8, 2.0, 5.0), (0.5, 7.0, 3.0)]:
        assert _beta_cdf(x, a, b) == pytest.approx(1.0 - _beta_cdf(1 - x, b, a), abs=1e-9)


# ===================================================== accrual posteriors ====
def test_no_posterior_is_reported_below_the_observation_floor() -> None:
    """The design is sequential, and a sequential design that reports a posterior at n=3 invites someone to read it."""
    assert posterior_non_inferiority(graded=3, cse=0, baseline=0.025, margin=0.01) is None
    assert posterior_non_inferiority(graded=29, cse=0, baseline=0.025, margin=0.01) is None
    assert posterior_non_inferiority(graded=30, cse=0, baseline=0.025, margin=0.01) is not None


def test_a_clean_run_accrues_toward_non_inferiority() -> None:
    """Observed 1.6% against a 2.5% baseline with a 1 pp margin."""
    posterior = posterior_non_inferiority(graded=500, cse=8, baseline=0.025, margin=0.01)
    assert posterior > POSTERIOR_THRESHOLD


def test_a_worse_than_baseline_run_does_not() -> None:
    """5% observed against a 3.5% threshold must not produce a confident non-inferiority claim however many reports back it."""
    posterior = posterior_non_inferiority(graded=3000, cse=150, baseline=0.025, margin=0.01)
    assert posterior < 0.01


def test_a_small_clean_sample_is_not_yet_convincing() -> None:
    """The whole point of the volume requirement: 100 reports at 1% is encouraging and not sufficient."""
    posterior = posterior_non_inferiority(graded=100, cse=1, baseline=0.025, margin=0.01)
    assert 0.5 < posterior < POSTERIOR_THRESHOLD


def test_more_evidence_at_the_same_rate_raises_confidence() -> None:
    """Monotonicity in n: the same observed rate over more reports is a stronger claim, which is what makes the sequential design work."""
    small = posterior_non_inferiority(graded=200, cse=4, baseline=0.025, margin=0.01)
    large = posterior_non_inferiority(graded=2000, cse=40, baseline=0.025, margin=0.01)
    assert large > small
