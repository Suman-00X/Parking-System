"""The interface every language-model provider implements, with the token and cost figures each call reports back.

Defines: LLMClient, a request and its reply (LLMRequest, LLMResponse, Usage), batch submissions
(BatchRequestItem) and the resolved choice of model (ResolvedModelRef).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from radreport.adapters.llm.prompt import Effort, PromptBundle


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as the provider reported them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def billable_input_tokens(self) -> int:
        """Uncached input. Cache reads bill at a small fraction, separately."""
        return self.input_tokens

    def cache_hit_ratio(self) -> float:
        total = self.input_tokens + self.cache_read_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0


@dataclass(slots=True)
class LLMResponse:
    text: str
    usage: Usage
    model_id: str
    """The **resolved** identifier actually sent. Plan: no date suffix — the API rejects those."""

    stop_reason: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    structured: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call. Assembled from a `PromptBundle`, never from a raw string."""

    prompt: PromptBundle
    max_tokens: int = 4096
    temperature: float | None = None
    seed: int | None = None

    effort: Effort | None = None
    """Sonnet 5 exposes `output_config.effort`."""

    thinking: bool = False
    """Emitted as `thinking: {type: "adaptive"}`. `budget_tokens` is rejected by the current API, and assistant prefill is removed."""

    json_schema: dict[str, Any] | None = None
    """The provider's JSON-schema mode: structured output uses `output_config: {format: {..}}`, and tool schemas take a top-level `strict: true`."""

    stop_sequences: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BatchRequestItem:
    """One item in a Batch API submission."""

    custom_id: str
    request: LLMRequest


class LLMClient(Protocol):
    """Provider-agnostic surface."""

    provider_name: str

    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse: ...

    async def submit_batch(self, items: list[BatchRequestItem], *, model_id: str) -> str:
        """Returns a batch id. 50% off input and output, ≤24h turnaround."""
        ...

    async def poll_batch(self, batch_id: str) -> str:
        """Provider `processing_status`; `"ended"` means results are ready."""
        ...

    async def fetch_batch_results(self, batch_id: str) -> dict[str, LLMResponse]:
        """Keyed by `custom_id` — never by position."""
        ...


@dataclass(frozen=True, slots=True)
class ResolvedModelRef:
    """Enough of a `model_definition` row to make and price a call."""

    model_definition_id: uuid.UUID
    model_identifier: str
    provider_name: str
    provider_kind: str
    endpoint: str | None
    input_price_per_1k: float
    output_price_per_1k: float
    cache_read_price_per_1k: float
    cache_write_price_per_1k: float
    batch_discount_factor: float = 1.0
    context_window: int | None = None
