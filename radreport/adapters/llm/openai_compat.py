"""Talks to any OpenAI-compatible endpoint, which is how locally hosted open-weight models are reached.

Defines: OpenAICompatibleClient, which implements the shared provider interface.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from radreport.adapters.llm.base import BatchRequestItem, LLMRequest, LLMResponse, ResolvedModelRef, Usage
from radreport.adapters.llm.concurrency import ProviderLimiter
from radreport.adapters.llm.pricing import cost_usd
from radreport.core.errors import ProviderError
from radreport.core.logging import get_logger

log = get_logger(__name__)


class OpenAICompatibleClient:
    """`/v1/chat/completions` against vLLM, Ollama, llama.cpp, LM Studio, …"""

    provider_name = "local_openai_compatible"

    def __init__(self, *, model_ref: ResolvedModelRef, api_key: str | None = None, limiter: ProviderLimiter | None = None, timeout_seconds: float = 120.0, client: httpx.AsyncClient | None = None) -> None:
        if not model_ref.endpoint:
            raise ValueError("a local model needs an endpoint: set `model_definition.endpoint_override` (per-tenant — each lab's box has its own address)")
        self._model_ref = model_ref
        self._base_url = model_ref.endpoint.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._limiter = limiter or ProviderLimiter(name="local_openai_compatible")
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
        started = time.perf_counter_ns()

        # No prompt caching on a local endpoint, so the stable/volatile split collapses back to one string.
        content = "\n\n".join(part for part in (request.prompt.stable_text(), request.prompt.volatile_text()) if part)

        payload: dict[str, Any] = {"model": model_id, "messages": [{"role": "user", "content": content}], "max_tokens": request.max_tokens}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "structured_output", "schema": request.json_schema, "strict": True}}
        if request.stop_sequences:
            payload["stop"] = list(request.stop_sequences)

        async def _call() -> httpx.Response:
            response = await self._client.post(f"{self._base_url}/v1/chat/completions", json=payload, headers=self._headers)
            response.raise_for_status()
            return response

        raw: Any = await self._limiter.call(_call)
        body = raw.json()
        latency_ms = (time.perf_counter_ns() - started) // 1_000_000

        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected response shape from {self._base_url}") from exc

        raw_usage = body.get("usage") or {}
        usage = Usage(input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0), output_tokens=int(raw_usage.get("completion_tokens", 0) or 0))

        response = LLMResponse(
            text=text,
            usage=usage,
            model_id=model_id,
            stop_reason=(body["choices"][0] or {}).get("finish_reason"),
            latency_ms=latency_ms,
            # Priced at the row's rate, which is 0 for a local model — but the
            # row still exists, so per-tenant metering stays uniform.
            cost_usd=cost_usd(usage, self._model_ref),
        )
        if request.json_schema is not None and text:
            try:
                response.structured = json.loads(text)
            except json.JSONDecodeError:
                response.structured = None
        return response

    async def submit_batch(self, items: list[BatchRequestItem], *, model_id: str) -> str:
        raise ProviderError("local endpoints have no Batch API; the 50% discount is a cloud-provider construct. Local inference is already at zero marginal cost, so batching buys nothing here.")

    async def poll_batch(self, batch_id: str) -> str:  # pragma: no cover
        raise ProviderError("local endpoints have no Batch API")

    async def fetch_batch_results(self, batch_id: str) -> dict[str, LLMResponse]:  # pragma: no cover
        raise ProviderError("local endpoints have no Batch API")

    async def aclose(self) -> None:
        await self._client.aclose()
