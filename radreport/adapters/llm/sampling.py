"""Asks the same question several times and keeps the answer most of the samples agree on.

Order: sample_k fires the requests, awaiting the first so the rest hit a warm cache; majority_vote
picks the winning answer out of the SampleSet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace

from radreport.adapters.llm.base import LLMClient, LLMRequest, LLMResponse
from radreport.core.logging import get_logger

log = get_logger(__name__)

#: k=2 on classes with proven low self-consistency disagreement saves ~20% of the LLM bill, but must clear the gate per class.
DEFAULT_K = 3
DEFAULT_TEMPERATURE = 0.3


@dataclass(frozen=True, slots=True)
class SampleSet:
    """k responses to one prompt, plus what the cache actually did."""

    responses: tuple[LLMResponse, ...]
    total_cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def warm_up_hit(self) -> bool:
        """Did the fan-out samples actually read the cache?"""
        return len(self.responses) > 1 and self.cache_read_tokens > 0


async def sample_k(client: LLMClient, request: LLMRequest, *, model_id: str, k: int = DEFAULT_K, temperature: float = DEFAULT_TEMPERATURE, seeds: Sequence[int] | None = None) -> SampleSet:
    """Run `k` samples of one prompt, warming the cache first."""
    if k < 1:
        raise ValueError("k must be >= 1")

    seed_list = list(seeds) if seeds is not None else list(range(k))
    if len(seed_list) < k:
        seed_list += list(range(len(seed_list), k))

    first = await client.complete(replace(request, temperature=temperature, seed=seed_list[0]), model_id=model_id)

    rest: list[LLMResponse] = []
    if k > 1:
        # Only now does the prefix exist in the cache.
        rest = list(await asyncio.gather(*(client.complete(replace(request, temperature=temperature, seed=seed_list[i]), model_id=model_id) for i in range(1, k))))

    responses = (first, *rest)
    cache_read = sum(r.usage.cache_read_input_tokens for r in responses)
    cache_write = sum(r.usage.cache_creation_input_tokens for r in responses)

    if k > 1 and cache_read == 0 and request.prompt.cache:
        log.warning("k_sample_cache_miss", k=k, model_id=model_id, detail=("fan-out samples read no cache; check that sample 1 is awaited before 2..k are fired"))

    return SampleSet(responses=responses, total_cost_usd=round(sum(r.cost_usd for r in responses), 8), cache_read_tokens=cache_read, cache_write_tokens=cache_write)


def majority_vote(values: Sequence[str | None]) -> tuple[str | None, float]:
    """Self-consistency agreement over k samples."""
    if not values:
        return None, 0.0
    counts: dict[str | None, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    winner, count = max(counts.items(), key=lambda kv: kv[1])
    return winner, count / len(values)
