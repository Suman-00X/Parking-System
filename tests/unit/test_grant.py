"""Granting the right to skip review, and withdrawing it when errors rise.

The asymmetry is the safety argument: granting is deliberate and hard, withdrawing is cheap.
These tests re-derive the tuning table the threshold was chosen from.
"""

from __future__ import annotations

import random

import pytest

from radreport.autonomy.grant import DEFAULT_CUSUM_THRESHOLD, DEGRADED_RATE_MULTIPLE, cusum_increment, step_cusum

BASELINE = 0.025


def _run(grades: list[bool], *, threshold: float = DEFAULT_CUSUM_THRESHOLD) -> tuple[float, int | None]:
    statistic, fired_at = 0.0, None
    for index, is_cse in enumerate(grades, start=1):
        step = step_cusum(current=statistic, is_cse=is_cse, baseline=BASELINE, threshold=threshold)
        statistic = step.statistic
        if step.signalled and fired_at is None:
            fired_at = index
    return statistic, fired_at


def _arl(rate: float, threshold: float, *, trials: int = 600, cap: int = 6000) -> float:
    """Average graded reports until the monitor signals."""
    rng = random.Random(7)
    lengths = []
    for _ in range(trials):
        statistic, n = 0.0, 0
        while n < cap:
            n += 1
            statistic = step_cusum(current=statistic, is_cse=rng.random() < rate, baseline=BASELINE, threshold=threshold).statistic
            if statistic >= threshold:
                break
        lengths.append(n)
    return sum(lengths) / len(lengths)


def test_a_clean_report_pays_the_statistic_down() -> None:
    """A run of good reports must actively reduce the statistic, not merely fail to raise it — otherwise a class accumulates toward revocation on ordinary noise."""
    assert cusum_increment(is_cse=False, acceptable=BASELINE, degraded=0.05) < 0
    assert cusum_increment(is_cse=True, acceptable=BASELINE, degraded=0.05) > 0


def test_the_statistic_is_floored_at_zero() -> None:
    """Letting a long clean run bank arbitrary credit would delay detection of a later degradation."""
    statistic, fired = _run([False] * 500)
    assert statistic == 0.0
    assert fired is None


def test_a_class_performing_at_baseline_is_not_revoked() -> None:
    """2.5% observed against a 2.5% baseline, spread evenly."""
    grades = ([False] * 39 + [True]) * 10  # 400 reports, 2.5%
    _statistic, fired = _run(grades)
    assert fired is None


def test_a_sustained_doubling_is_revoked() -> None:
    """The case the monitor exists for: a real drift to 5%, never spiking."""
    grades = ([False] * 19 + [True]) * 30  # 600 reports, 5%
    _statistic, fired = _run(grades)
    assert fired is not None
    assert fired < 400, "a sustained doubling should be caught within a few hundred"


def test_a_burst_of_errors_is_revoked_quickly() -> None:
    """Five clinically significant errors in a row is not ordinary noise."""
    _statistic, fired = _run([False] * 100 + [True] * 6)
    assert fired is not None
    assert fired <= 106


def test_the_monitor_detects_a_slow_drift_a_rolling_window_would_miss() -> None:
    """A rolling rate answers "was the last N above threshold?", which is insensitive to a drift that never spikes."""
    grades = ([False] * 14 + [True]) * 30  # ~6.7%, evenly spread
    _statistic, fired = _run(grades)
    assert fired is not None


def test_the_threshold_sits_where_the_run_lengths_say_it_should() -> None:
    """Re-derives the operating point the default was chosen from."""
    in_control = _arl(BASELINE, DEFAULT_CUSUM_THRESHOLD)
    doubled = _arl(BASELINE * DEGRADED_RATE_MULTIPLE, DEFAULT_CUSUM_THRESHOLD)

    assert in_control > 1500, f"too jumpy: false revocation every {in_control:.0f} reports"
    assert doubled < 500, f"too slow: a doubling takes {doubled:.0f} reports to catch"
    assert in_control > doubled * 4, "the monitor must separate the two rates clearly"


def test_a_higher_threshold_is_less_sensitive_in_both_directions() -> None:
    """Monotonicity — a sanity check on the whole scheme."""
    assert _arl(BASELINE, 4.0) > _arl(BASELINE, 2.0)
    assert _arl(0.05, 4.0) > _arl(0.05, 2.0)


@pytest.mark.parametrize("rate", [0.0, 0.5, 1.0])
def test_the_increment_is_finite_at_the_extremes(rate: float) -> None:
    """A baseline of 0 or 1 must not produce infinities; the class would be unmonitorable rather than perfectly safe."""
    value = cusum_increment(is_cse=True, acceptable=rate, degraded=min(0.99, rate * 2))
    assert value == value  # not NaN
    assert abs(value) < 100
