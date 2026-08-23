"""Working out what a model call cost, including the cached and batched rates."""

from __future__ import annotations

import uuid

import pytest

from radreport.adapters.llm.base import ResolvedModelRef, Usage
from radreport.adapters.llm.pricing import SEED_PRICES, cost_usd, derive_cache_prices, savings_vs_uncached


def _ref(input_price: float = 0.002, output_price: float = 0.010) -> ResolvedModelRef:
    read, write = derive_cache_prices(input_price)
    return ResolvedModelRef(model_definition_id=uuid.uuid4(), model_identifier="claude-sonnet-5", provider_name="anthropic", provider_kind="cloud_api", endpoint=None, input_price_per_1k=input_price, output_price_per_1k=output_price, cache_read_price_per_1k=read, cache_write_price_per_1k=write, batch_discount_factor=0.5)


def test_sonnet_5_carries_the_corrected_rate() -> None:
    """Plan: prices Sonnet 5 at $3/$15, which was Sonnet 4.6's rate."""
    sonnet = SEED_PRICES["claude-sonnet-5"]
    assert (sonnet.input_per_1k, sonnet.output_per_1k) == (0.002, 0.010)

    opus = SEED_PRICES["claude-opus-5"]
    assert opus.input_per_1k / sonnet.input_per_1k == pytest.approx(2.5)


def test_model_identifiers_carry_no_date_suffix() -> None:
    """Plan: the `claude-sonnet-5-20260415` is rejected by the API."""
    for identifier in SEED_PRICES:
        tail = identifier.rsplit("-", 1)[-1]
        assert not (len(tail) == 8 and tail.isdigit()), f"{identifier} carries a date suffix"


def test_cache_reads_are_billed_separately_from_fresh_input() -> None:
    """Without the split, the Tier-1 saving is invisible in `stage_execution.cost_usd` — and Tier 1 is ~29% of the LLM bill."""
    ref = _ref()
    fresh = cost_usd(Usage(input_tokens=10_000, output_tokens=500), ref)
    cached = cost_usd(Usage(input_tokens=0, output_tokens=500, cache_read_input_tokens=10_000), ref)
    assert cached < fresh
    # Cache reads are ~10% of base input.
    assert (fresh - cached) == pytest.approx(10_000 / 1000 * 0.002 * 0.9, rel=1e-6)


def test_cache_write_costs_more_than_plain_input() -> None:
    """Sample 1 pays a premium to write the entry; 2 and 3 more than repay it."""
    ref = _ref()
    write = cost_usd(Usage(cache_creation_input_tokens=10_000), ref)
    plain = cost_usd(Usage(input_tokens=10_000), ref)
    assert write > plain


def test_k3_with_caching_beats_k3_without() -> None:
    """The Tier-1 arithmetic, end to end."""
    ref = _ref()
    prefix = 50_000
    uncached = 3 * cost_usd(Usage(input_tokens=prefix, output_tokens=400), ref)
    cached = cost_usd(Usage(cache_creation_input_tokens=prefix, output_tokens=400), ref) + 2 * cost_usd(Usage(cache_read_input_tokens=prefix, output_tokens=400), ref)
    assert cached < uncached
    assert (uncached - cached) / uncached > 0.35


def test_batch_api_halves_the_bill() -> None:
    """Same models, same outputs; the only thing traded is immediacy."""
    ref = _ref()
    usage = Usage(input_tokens=10_000, output_tokens=1_000)
    assert cost_usd(usage, ref, batched=True) == pytest.approx(cost_usd(usage, ref) * 0.5, rel=1e-9)


def test_local_models_price_at_zero_without_special_casing() -> None:
    """Local models are just rows, with no separate code path."""
    ref = _ref(input_price=0.0, output_price=0.0)
    assert cost_usd(Usage(input_tokens=100_000, output_tokens=10_000), ref) == 0.0


def test_savings_are_reported_not_assumed() -> None:
    """Plan item 4 makes the cacheable fraction a measurement."""
    ref = _ref()
    assert savings_vs_uncached(Usage(input_tokens=1000), ref) == 0.0
    assert savings_vs_uncached(Usage(cache_read_input_tokens=10_000), ref) > 0
