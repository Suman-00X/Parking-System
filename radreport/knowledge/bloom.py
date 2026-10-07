"""A Bloom filter: a compact set that answers "definitely not present" or "maybe present".

Defines: BloomFilter, sized from the expected item count and the false-positive rate wanted
(BloomFilter.for_capacity), with add, "in", and the rate it is running at now (estimated_fp_rate).
No dependency: double hashing over one blake2b digest gives the k bit positions.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Iterable


class BloomFilter:
    """No false negatives, ever; false positives at about the configured rate while under capacity."""

    def __init__(self, *, bits: int, hashes: int) -> None:
        if bits < 8 or hashes < 1:
            raise ValueError("a Bloom filter needs at least 8 bits and one hash")
        self.bits = bits
        self.hashes = hashes
        self._array = bytearray((bits + 7) // 8)
        self.count = 0
        self._lock = threading.Lock()

    @classmethod
    def for_capacity(cls, expected_items: int, fp_rate: float = 0.01) -> BloomFilter:
        """The optimal size for `expected_items` at `fp_rate`: m = -n ln p / (ln 2)^2, k = (m/n) ln 2."""
        if not 0 < fp_rate < 1:
            raise ValueError("fp_rate must be between 0 and 1")
        n = max(1, expected_items)
        m = max(64, math.ceil(-n * math.log(fp_rate) / (math.log(2) ** 2)))
        k = max(1, round(m / n * math.log(2)))
        return cls(bits=m, hashes=k)

    @classmethod
    def of(cls, items: Iterable[str], *, fp_rate: float = 0.01, headroom: float = 2.0) -> BloomFilter:
        """A filter holding `items`, sized with room to grow before the rate degrades."""
        values = list(items)
        bloom = cls.for_capacity(int(len(values) * headroom) + 1000, fp_rate)
        for value in values:
            bloom.add(value)
        return bloom

    def _positions(self, item: str) -> list[int]:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1
        return [(h1 + i * h2) % self.bits for i in range(self.hashes)]

    def add(self, item: str) -> None:
        with self._lock:
            for position in self._positions(item):
                self._array[position >> 3] |= 1 << (position & 7)
            self.count += 1

    def __contains__(self, item: str) -> bool:
        return all(self._array[p >> 3] & (1 << (p & 7)) for p in self._positions(item))

    def estimated_fp_rate(self) -> float:
        """(1 - e^(-kn/m))^k for the items added so far."""
        return (1 - math.exp(-self.hashes * self.count / self.bits)) ** self.hashes
