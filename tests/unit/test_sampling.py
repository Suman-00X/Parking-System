"""The sampling order that lets the second and later calls hit a warm provider cache."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from radreport.adapters.llm.base import LLMRequest, LLMResponse, Usage
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, system_block
from radreport.adapters.llm.sampling import majority_vote, sample_k


@dataclass
class RecordingClient:
    """Fake client that models the cache: an entry exists only once a call has *completed*, which is precisely the behaviour the trap depends on."""

    provider_name: str = "fake"
    concurrent: int = 0
    max_concurrent: int = 0
    completed_prefixes: set[str] = field(default_factory=set)
    order: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=lambda: ["A", "A", "B"])
    _calls: int = 0

    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
        index = self._calls
        self._calls += 1
        key = request.prompt.cache_key()

        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.order.append(f"start:{index}")
        try:
            await asyncio.sleep(0.01)
            hit = key in self.completed_prefixes
        finally:
            self.concurrent -= 1
            self.order.append(f"end:{index}")

        self.completed_prefixes.add(key)
        return LLMResponse(text=self.texts[index % len(self.texts)], usage=Usage(input_tokens=0 if hit else 1000, output_tokens=50, cache_read_input_tokens=1000 if hit else 0, cache_creation_input_tokens=0 if hit else 1000), model_id=model_id)


def _request() -> LLMRequest:
    return LLMRequest(prompt=PromptBundle(stable=[system_block("stable prefix")], volatile=[VolatileBlock(text="transcript")]))


async def test_first_sample_completes_before_the_rest_start() -> None:
    """The ordering, asserted directly on the call trace."""
    client = RecordingClient()
    await sample_k(client, _request(), model_id="claude-sonnet-5", k=3)

    assert client.order[0] == "start:0"
    assert client.order[1] == "end:0", "sample 1 must complete before 2 and 3 start; firing all k concurrently means 2 and 3 arrive before the cache entry exists and all three miss"


async def test_fan_out_samples_read_the_cache() -> None:
    client = RecordingClient()
    result = await sample_k(client, _request(), model_id="claude-sonnet-5", k=3)

    assert len(result.responses) == 3
    assert result.cache_read_tokens == 2000, "samples 2 and 3 should both hit"
    assert result.cache_write_tokens == 1000, "only sample 1 writes the entry"
    assert result.warm_up_hit


async def test_naive_gather_would_miss_every_cache_entry() -> None:
    """The counter-example, so the assertion above has something to mean."""
    client = RecordingClient()
    request = _request()
    await asyncio.gather(*(client.complete(request, model_id="claude-sonnet-5") for _ in range(3)))
    assert client.max_concurrent == 3
    assert not client.completed_prefixes - {request.prompt.cache_key()}
    # All three started before any finished, so none could have read a cache.
    assert client.order[:3] == ["start:0", "start:1", "start:2"]


async def test_samples_two_and_three_run_concurrently() -> None:
    """The warm-up costs one extra round trip, not k-1 of them."""
    client = RecordingClient()
    await sample_k(client, _request(), model_id="claude-sonnet-5", k=3)
    assert client.max_concurrent == 2


async def test_k_of_one_makes_no_fan_out() -> None:
    """Plan allows dropping k per template class; k=1 must still work."""
    client = RecordingClient()
    result = await sample_k(client, _request(), model_id="claude-sonnet-5", k=1)
    assert len(result.responses) == 1
    assert not result.warm_up_hit


async def test_k_zero_is_rejected() -> None:
    client = RecordingClient()
    with pytest.raises(ValueError):
        await sample_k(client, _request(), model_id="claude-sonnet-5", k=0)


async def test_each_sample_gets_a_distinct_seed() -> None:
    """Varies only temperature and seed — identical prompts otherwise, which is exactly why the prefix repeats are cacheable."""
    seen: list[int | None] = []

    class SeedClient(RecordingClient):
        async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
            seen.append(request.seed)
            return await super().complete(request, model_id=model_id)

    await sample_k(SeedClient(), _request(), model_id="claude-sonnet-5", k=3)
    assert len(set(seen)) == 3


def test_majority_vote_reports_agreement() -> None:
    """Feeds the confidence aggregation."""
    assert majority_vote(["a", "a", "b"]) == ("a", pytest.approx(2 / 3))
    assert majority_vote(["a", "a", "a"]) == ("a", 1.0)
    assert majority_vote([]) == (None, 0.0)


def test_majority_vote_surfaces_disagreement_rather_than_hiding_it() -> None:
    """Three-way disagreement gives 1/3 agreement, not a confident winner."""
    _winner, agreement = majority_vote(["a", "b", "c"])
    assert agreement == pytest.approx(1 / 3)
