"""Detecting that the system's behaviour has shifted between two time windows."""

from __future__ import annotations

import random

import pytest

from radreport.monitoring.drift import MIN_SAMPLE, PSI_SIGNIFICANT, categorical_drift, population_stability_index


def _sample(values: list[float], n: int, seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    return [rng.choice(values) for _ in range(n)]


def test_an_unchanged_distribution_is_stable() -> None:
    baseline = _sample([0.2, 0.5, 0.8, 0.95], 400, seed=1)
    current = _sample([0.2, 0.5, 0.8, 0.95], 400, seed=2)
    result = population_stability_index(baseline, current)

    assert result.psi < 0.10
    assert result.band == "stable"


def test_a_shift_toward_low_confidence_is_significant() -> None:
    """The case that matters: the pipeline getting less sure of itself."""
    baseline = _sample([0.85, 0.9, 0.95], 400, seed=1)
    current = _sample([0.2, 0.35, 0.5], 400, seed=2)
    result = population_stability_index(baseline, current)

    assert result.psi >= PSI_SIGNIFICANT
    assert result.is_significant


def test_a_split_distribution_is_caught_where_a_mean_would_not_be() -> None:
    """A bimodal split can leave the mean untouched."""
    baseline = [0.6] * 400
    current = [0.2] * 200 + [1.0] * 200  # same mean, different shape

    assert sum(baseline) / len(baseline) == pytest.approx(sum(current) / len(current), abs=0.02)
    result = population_stability_index(baseline, current)
    assert result.is_significant


def test_no_psi_is_reported_below_the_sample_floor() -> None:
    """A PSI from 12 reports will be quoted later without its n."""
    small = [0.5] * (MIN_SAMPLE - 1)
    assert population_stability_index(small, small) is None
    assert population_stability_index([0.5] * 200, small) is None


def test_a_new_category_does_not_produce_infinite_drift() -> None:
    """A bucket empty in the baseline and populated now would divide by zero; a single new template would otherwise read as catastrophic drift."""
    baseline = ["CT_CHEST"] * 200 + ["USG_ABDO"] * 200
    current = baseline[:399] + ["MRI_BRAIN"]

    result = categorical_drift(baseline, current, metric="template_mix")
    assert result.psi is not None
    assert result.psi < 0.10, "one new template out of 400 is not significant drift"


def test_a_real_change_in_the_template_mix_is_caught() -> None:
    """A shift in which studies are being reported can mean a new scanner, a new referrer, or routing making a different mistake."""
    baseline = ["CT_CHEST"] * 350 + ["USG_ABDO"] * 50
    current = ["CT_CHEST"] * 50 + ["USG_ABDO"] * 350

    result = categorical_drift(baseline, current, metric="template_mix")
    assert result.is_significant


def test_new_capture_hardware_shows_as_drift() -> None:
    """A new microphone on someone's desk changes the distribution the engine was chosen against."""
    baseline = ["legacy"] * 400
    current = ["legacy"] * 200 + ["dictation_mic_ptt"] * 200

    result = categorical_drift(baseline, current, metric="capture_device_class")
    assert result.is_significant
    assert result.current_distribution["dictation_mic_ptt"] == pytest.approx(0.5)


def test_psi_is_reported_with_both_denominators() -> None:
    """A drift number without its sample sizes gets quoted on its own."""
    result = population_stability_index([0.5] * 200, [0.5] * 300)
    assert (result.baseline_n, result.current_n) == (200, 300)
