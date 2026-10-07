"""The consistent-hash ring: stable, even, and adding a shard moves only its share of labs."""

from __future__ import annotations

import uuid
from collections import Counter

import pytest

from radreport.db.sharding import HashRing

LABS = [uuid.UUID(int=n * 7919 + 1) for n in range(6000)]


def test_a_lab_always_lands_on_the_same_shard() -> None:
    ring = HashRing(["a", "b", "c"])
    again = HashRing(["c", "a", "b"])
    assert all(ring.shard_for(lab) == again.shard_for(lab) for lab in LABS[:500])


def test_labs_spread_evenly() -> None:
    counts = Counter(HashRing(["a", "b", "c", "d"]).shard_for(lab) for lab in LABS)
    assert set(counts) == {"a", "b", "c", "d"}
    assert max(counts.values()) / min(counts.values()) < 1.3, counts


@pytest.mark.parametrize("before", [2, 3, 5])
def test_adding_a_shard_moves_about_one_in_n(before: int) -> None:
    names = [f"s{i}" for i in range(before)]
    old, new = HashRing(names), HashRing([*names, "added"])
    moved = [lab for lab in LABS if old.shard_for(lab) != new.shard_for(lab)]
    share = len(moved) / len(LABS)
    assert abs(share - 1 / (before + 1)) < 0.05, share
    assert all(new.shard_for(lab) == "added" for lab in moved), "labs only ever move onto the new shard"


def test_a_ring_needs_a_shard() -> None:
    with pytest.raises(ValueError):
        HashRing([])
