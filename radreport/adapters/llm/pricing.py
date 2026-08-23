"""Works out what a call cost, including the cheaper cached and batched rates.

Order: price one call (cost_usd) -> derive the cache rates from the base price
(derive_cache_prices) -> report what caching saved (savings_vs_uncached).
"""

from __future__ import annotations

from dataclasses import dataclass

from radreport.adapters.llm.base import ResolvedModelRef, Usage

#: USD per 1K tokens. Cache reads are ~0.1× base input; cache writes ~1.25×.
#: Anthropic's published multipliers for a 5-minute ephemeral entry.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

#: same models, same outputs; the only thing traded is immediacy.
BATCH_DISCOUNT = 0.50


@dataclass(frozen=True, slots=True)
class SeedPrice:
    input_per_1k: float
    output_per_1k: float
    context_window: int


#: Plan-corrected rates, keyed by the valid identifiers (no date suffixes).
#: Context windows: Opus 5 and Sonnet 5 are 1M; Haiku 4.5 is 200K.
SEED_PRICES: dict[str, SeedPrice] = {"claude-opus-5": SeedPrice(0.005, 0.025, 1_000_000), "claude-sonnet-5": SeedPrice(0.002, 0.010, 1_000_000), "claude-haiku-4-5": SeedPrice(0.001, 0.005, 200_000)}


def cost_usd(usage: Usage, model: ResolvedModelRef, *, batched: bool = False) -> float:
    """Price one call."""
    discount = model.batch_discount_factor if batched else 1.0

    fresh_input = usage.input_tokens / 1000 * model.input_price_per_1k
    cached_input = usage.cache_read_input_tokens / 1000 * model.cache_read_price_per_1k
    written_cache = usage.cache_creation_input_tokens / 1000 * model.cache_write_price_per_1k
    output = usage.output_tokens / 1000 * model.output_price_per_1k

    return round((fresh_input + cached_input + written_cache + output) * discount, 8)


def derive_cache_prices(input_per_1k: float) -> tuple[float, float]:
    """(read, write) prices for a model that did not declare them."""
    return (round(input_per_1k * CACHE_READ_MULTIPLIER, 8), round(input_per_1k * CACHE_WRITE_MULTIPLIER, 8))


def savings_vs_uncached(usage: Usage, model: ResolvedModelRef) -> float:
    """What the cache actually saved on this call, in USD."""
    if not usage.cache_read_input_tokens:
        return 0.0
    full_price = usage.cache_read_input_tokens / 1000 * model.input_price_per_1k
    paid = usage.cache_read_input_tokens / 1000 * model.cache_read_price_per_1k
    return round(full_price - paid, 8)
