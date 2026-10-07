"""The Bloom filter: never a false negative, and false positives near the rate it was sized for."""

from __future__ import annotations

import math

import pytest

from radreport.knowledge.bloom import BloomFilter


def test_sizing_follows_the_standard_formula() -> None:
    bloom = BloomFilter.for_capacity(10_000, 0.01)
    assert bloom.bits == math.ceil(-10_000 * math.log(0.01) / math.log(2) ** 2)
    assert bloom.hashes == 7


def test_no_false_negatives() -> None:
    items = [f"sha256:{n:064x}" for n in range(20_000)]
    bloom = BloomFilter.of(items, fp_rate=0.01, headroom=1.0)
    assert all(item in bloom for item in items)


@pytest.mark.parametrize("target", [0.01, 0.001])
def test_the_measured_false_positive_rate_is_within_target(target: float) -> None:
    bloom = BloomFilter.for_capacity(20_000, target)
    for n in range(20_000):
        bloom.add(f"present-{n}")
    probes = 200_000
    false_positives = sum(f"absent-{n}" in bloom for n in range(probes))
    measured = false_positives / probes
    assert measured <= target * 1.5, f"measured {measured:.4%} against a {target:.2%} target"
    assert bloom.estimated_fp_rate() == pytest.approx(target, rel=0.3)


def test_bad_sizes_are_refused() -> None:
    with pytest.raises(ValueError):
        BloomFilter.for_capacity(10, 1.5)
    with pytest.raises(ValueError):
        BloomFilter(bits=4, hashes=1)
