"""Spike detection on daily spend: a real jump is flagged, a ramp-up and a noisy week are not."""

from __future__ import annotations

import datetime as dt

from radreport.monitoring.costs import detect_spikes, series

DAY = dt.date(2026, 9, 1)


def _points(values: list[float]) -> list[tuple[dt.date, float]]:
    return [(DAY + dt.timedelta(days=i), v) for i, v in enumerate(values)]


def test_a_jump_far_above_the_trailing_window_is_a_spike() -> None:
    values = [1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.1, 0.9, 4.8]
    [spike] = detect_spikes(_points(values))
    assert spike.day == DAY + dt.timedelta(days=14) and spike.ratio > 4


def test_normal_variation_is_not_a_spike() -> None:
    values = [1.0, 1.4, 0.7, 1.2, 0.8, 1.3, 0.9, 1.1, 1.0, 1.5, 0.6, 1.2, 0.9, 1.0, 1.6]
    assert detect_spikes(_points(values)) == []


def test_a_new_lab_ramping_up_is_not_a_spike() -> None:
    values = [0.0] * 12 + [0.5, 1.0, 3.0]
    assert detect_spikes(_points(values)) == []


def test_small_absolute_jumps_are_ignored() -> None:
    values = [0.02] * 14 + [0.4]
    assert detect_spikes(_points(values)) == []


def test_days_without_runs_are_zeros_in_the_series() -> None:
    rows = [{"day": DAY, "cost_usd": 1.0}, {"day": DAY + dt.timedelta(days=2), "cost_usd": 2.0}, {"day": DAY + dt.timedelta(days=2), "cost_usd": 0.5}]
    assert series(rows, start=DAY, end=DAY + dt.timedelta(days=2)) == [(DAY, 1.0), (DAY + dt.timedelta(days=1), 0.0), (DAY + dt.timedelta(days=2), 2.5)]
