"""The model response cache: repeats are free, samples stay distinct, varying requests are never cached, and labs never share replies."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from radreport.adapters.llm import response_cache
from radreport.adapters.llm.base import LLMRequest, LLMResponse, Usage
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, system_block
from radreport.adapters.llm.sampling import sample_k

pytestmark = pytest.mark.db


class CountingClient:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=f"answer seed={request.seed} call={self.calls}", usage=Usage(input_tokens=1000, output_tokens=200), model_id=model_id, cost_usd=0.012)


def _request(text: str = "the liver is normal", **overrides) -> LLMRequest:  # type: ignore[no-untyped-def]
    return LLMRequest(prompt=PromptBundle(stable=[system_block("You extract findings.")], volatile=[VolatileBlock(label="transcript", text=text)]), **overrides)


@pytest.fixture(params=["postgres", "shared"])
def store(request, migrated_db: str):  # type: ignore[no-untyped-def]
    return response_cache.PostgresResponseStore(migrated_db) if request.param == "postgres" else response_cache.SharedResponseStore()


def test_a_repeat_is_answered_from_the_cache_for_free(store, two_tenants) -> None:  # type: ignore[no-untyped-def]
    lab, _ = two_tenants
    inner = CountingClient()
    client = response_cache.CachingLLMClient(inner, tenant_id=lab, store=store, ttl_hours=1)
    first = asyncio.run(client.complete(_request(temperature=0), model_id="claude-sonnet-5"))
    again = asyncio.run(client.complete(_request(temperature=0), model_id="claude-sonnet-5"))
    assert inner.calls == 1
    assert again.text == first.text and again.cost_usd == 0.0 and again.raw["original_cost_usd"] == 0.012


def test_a_different_input_or_model_is_a_different_entry(store, two_tenants) -> None:  # type: ignore[no-untyped-def]
    lab, _ = two_tenants
    inner = CountingClient()
    client = response_cache.CachingLLMClient(inner, tenant_id=lab, store=store, ttl_hours=1)
    asyncio.run(client.complete(_request(f"text {uuid.uuid4()}", temperature=0), model_id="claude-sonnet-5"))
    asyncio.run(client.complete(_request(f"text {uuid.uuid4()}", temperature=0), model_id="claude-sonnet-5"))
    asyncio.run(client.complete(_request("same", temperature=0), model_id="claude-sonnet-5"))
    asyncio.run(client.complete(_request("same", temperature=0), model_id="claude-opus-5"))
    assert inner.calls == 4


def test_k_samples_stay_k_answers_and_replay_together(store, two_tenants) -> None:  # type: ignore[no-untyped-def]
    """The disagreement between samples is a signal; caching must not collapse it."""
    lab, _ = two_tenants
    inner = CountingClient()
    client = response_cache.CachingLLMClient(inner, tenant_id=lab, store=store, ttl_hours=1)
    text = f"dictation {uuid.uuid4()}"
    first = asyncio.run(sample_k(client, _request(text), model_id="claude-sonnet-5", k=3))
    second = asyncio.run(sample_k(client, _request(text), model_id="claude-sonnet-5", k=3))
    assert inner.calls == 3
    assert len({r.text for r in first.responses}) == 3
    assert [r.text for r in second.responses] == [r.text for r in first.responses]


def test_a_request_meant_to_vary_is_never_cached(store, two_tenants) -> None:  # type: ignore[no-untyped-def]
    lab, _ = two_tenants
    inner = CountingClient()
    client = response_cache.CachingLLMClient(inner, tenant_id=lab, store=store, ttl_hours=1)
    for _ in range(2):
        asyncio.run(client.complete(_request(temperature=0.7), model_id="claude-sonnet-5"))
    assert inner.calls == 2


def test_one_labs_replies_are_never_served_to_another(store, two_tenants) -> None:  # type: ignore[no-untyped-def]
    lab_a, lab_b = two_tenants
    inner = CountingClient()
    text = f"shared wording {uuid.uuid4()}"
    asyncio.run(response_cache.CachingLLMClient(inner, tenant_id=lab_a, store=store, ttl_hours=1).complete(_request(text, temperature=0), model_id="m"))
    asyncio.run(response_cache.CachingLLMClient(inner, tenant_id=lab_b, store=store, ttl_hours=1).complete(_request(text, temperature=0), model_id="m"))
    assert inner.calls == 2


def test_a_store_outage_falls_back_to_the_model(two_tenants) -> None:
    lab, _ = two_tenants

    class Down:
        def get(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

        def put(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

    inner = CountingClient()
    reply = asyncio.run(response_cache.CachingLLMClient(inner, tenant_id=lab, store=Down(), ttl_hours=1).complete(_request(temperature=0), model_id="m"))
    assert inner.calls == 1 and reply.cost_usd == 0.012


def test_the_setting_chooses_the_store(monkeypatch: pytest.MonkeyPatch, two_tenants) -> None:
    from radreport.core.config import get_settings

    lab, _ = two_tenants
    inner = CountingClient()
    for value, expected in (("off", CountingClient), ("postgres", response_cache.CachingLLMClient), ("shared", response_cache.CachingLLMClient)):
        monkeypatch.setenv("RADREPORT_LLM__RESPONSE_CACHE", value)
        get_settings.cache_clear()
        assert isinstance(response_cache.with_response_cache(inner, tenant_id=lab), expected)
    get_settings.cache_clear()
