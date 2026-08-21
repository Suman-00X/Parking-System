"""Talks to Anthropic's API, including its caching and batch endpoints.

Defines: AnthropicClient, which implements the shared provider interface.
"""

from __future__ import annotations

import time
from typing import Any

from radreport.adapters.llm.base import BatchRequestItem, LLMRequest, LLMResponse, ResolvedModelRef, Usage
from radreport.adapters.llm.concurrency import ProviderLimiter
from radreport.adapters.llm.pricing import cost_usd
from radreport.core.errors import ProviderError
from radreport.core.logging import get_logger

log = get_logger(__name__)


class AnthropicClient:
    """Wraps the official SDK with cost accounting and backpressure."""

    provider_name = "anthropic"

    def __init__(self, api_key: str, *, model_ref: ResolvedModelRef, limiter: ProviderLimiter | None = None, client: Any | None = None) -> None:
        self._model_ref = model_ref
        self._limiter = limiter or ProviderLimiter(name="anthropic")
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key)

    # ------------------------------------------------------------- single ---
    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
        started = time.perf_counter_ns()
        payload = self._build_payload(request, model_id)

        async def _call() -> Any:
            return await self._client.messages.create(**payload)

        raw = await self._limiter.call(_call)
        latency_ms = (time.perf_counter_ns() - started) // 1_000_000
        return self._to_response(raw, model_id, latency_ms)

    def _build_payload(self, request: LLMRequest, model_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model_id, "max_tokens": request.max_tokens, "messages": [{"role": "user", "content": request.prompt.to_content_blocks()}]}

        if request.temperature is not None:
            payload["temperature"] = request.temperature

        # adaptive thinking, no budget_tokens.
        if request.thinking:
            payload["thinking"] = {"type": "adaptive"}

        output_config: dict[str, Any] = {}
        if request.effort is not None:
            # extraction at `medium` rather than the default cuts thinking
            # tokens. Free to test, measurable on the gold set.
            output_config["effort"] = request.effort
        if request.json_schema is not None:
            # the constrained decoding, in its current spelling.
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema, "strict": True}
        if output_config:
            payload["output_config"] = output_config

        if request.stop_sequences:
            payload["stop_sequences"] = list(request.stop_sequences)
        if request.metadata:
            payload["metadata"] = request.metadata

        return payload

    def _to_response(self, raw: Any, model_id: str, latency_ms: int) -> LLMResponse:
        usage = _extract_usage(raw)
        text = _extract_text(raw)
        response = LLMResponse(text=text, usage=usage, model_id=model_id, stop_reason=getattr(raw, "stop_reason", None), latency_ms=latency_ms)
        response.cost_usd = cost_usd(usage, self._model_ref)
        return response

    # -------------------------------------------------------------- batch ---
    async def submit_batch(self, items: list[BatchRequestItem], *, model_id: str) -> str:
        """50% off, ≤24h. Never for critical findings or stat/urgent."""
        requests = [{"custom_id": item.custom_id, "params": self._build_payload(item.request, model_id)} for item in items]
        batch = await self._limiter.call(lambda: self._client.messages.batches.create(requests=requests))
        log.info("batch_submitted", batch_id=batch.id, item_count=len(items), model_id=model_id)
        return str(batch.id)

    async def poll_batch(self, batch_id: str) -> str:
        batch = await self._limiter.call(lambda: self._client.messages.batches.retrieve(batch_id))
        return str(batch.processing_status)

    async def fetch_batch_results(self, batch_id: str) -> dict[str, LLMResponse]:
        """Keyed by `custom_id`."""
        results: dict[str, LLMResponse] = {}
        stream = await self._limiter.call(lambda: self._client.messages.batches.results(batch_id))
        async for entry in _aiter(stream):
            custom_id = getattr(entry, "custom_id", None)
            if custom_id is None:
                raise ProviderError("batch result without custom_id; cannot attribute")
            message = getattr(getattr(entry, "result", None), "message", None)
            if message is None:
                log.warning("batch_item_failed", batch_id=batch_id, custom_id=custom_id)
                continue
            usage = _extract_usage(message)
            response = LLMResponse(text=_extract_text(message), usage=usage, model_id=str(getattr(message, "model", "")), stop_reason=getattr(message, "stop_reason", None))
            response.cost_usd = cost_usd(usage, self._model_ref, batched=True)
            results[str(custom_id)] = response
        return results


# ------------------------------------------------------------------ helpers --
def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _extract_usage(message: Any) -> Usage:
    usage = getattr(message, "usage", None)
    if usage is None:
        return Usage()
    return Usage(input_tokens=int(getattr(usage, "input_tokens", 0) or 0), output_tokens=int(getattr(usage, "output_tokens", 0) or 0), cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0), cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0))


async def _aiter(stream: Any):  # noqa: ANN202
    """Iterate a batch result stream whether the SDK returns sync or async."""
    if hasattr(stream, "__aiter__"):
        async for item in stream:
            yield item
    else:
        for item in stream:
            yield item
