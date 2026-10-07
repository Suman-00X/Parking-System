"""Answers a repeat of an identical model request from a stored reply, so re-running a recording does not pay twice.

Order: fingerprint a request with everything that changes its answer (request_fingerprint) ->
decide whether it may be cached at all (cacheable: a sampled request with no seed is meant to
vary, so it never is) -> look up, else call and store (CachingLLMClient.complete) in one of two
stores, a lab-isolated Postgres table (PostgresResponseStore) or the shared cache
(SharedResponseStore) -> wrap a client as configured (with_response_cache). A hit costs nothing
and reports what it saved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from radreport.adapters.llm.base import BatchRequestItem, LLMClient, LLMRequest, LLMResponse, Usage
from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)


def request_fingerprint(request: LLMRequest, *, model_id: str) -> str:
    """sha256 over the model and every field of the request that can change the reply."""
    body = {"model": model_id, "stable": request.prompt.stable_text(), "volatile": request.prompt.volatile_text(), "max_tokens": request.max_tokens, "temperature": request.temperature, "seed": request.seed, "effort": str(request.effort) if request.effort is not None else None, "thinking": request.thinking, "schema": request.json_schema, "stop": list(request.stop_sequences)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def cacheable(request: LLMRequest) -> bool:
    """Deterministic requests, and seeded samples (each seed is its own entry, so k samples stay k different answers)."""
    return not request.temperature or request.seed is not None


def _encode(response: LLMResponse) -> dict[str, Any]:
    return {"text": response.text, "usage": asdict(response.usage), "model_id": response.model_id, "stop_reason": response.stop_reason, "cost_usd": response.cost_usd, "structured": response.structured}


def _decode(raw: dict[str, Any]) -> LLMResponse:
    # A replayed answer costs nothing and reads no tokens; what the original cost is kept so the saving can be reported.
    return LLMResponse(text=raw["text"], usage=Usage(), model_id=raw["model_id"], stop_reason=raw.get("stop_reason"), cost_usd=0.0, latency_ms=0, structured=raw.get("structured"), raw={"response_cache": "hit", "original_cost_usd": raw.get("cost_usd", 0.0), "original_usage": raw.get("usage")})


#: The share of writes that also sweep the lab's expired replies.
PURGE_EVERY = 0.005


class ResponseStore(Protocol):
    def get(self, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None: ...
    def put(self, tenant_id: uuid.UUID, key: str, value: dict[str, Any], *, model_id: str, task_key: str | None, ttl_hours: float) -> None: ...


class PostgresResponseStore:
    """llm_response_cache, written and read from a session bound to the lab, so row-level security keeps labs apart."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url

    def get(self, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        from sqlalchemy import text

        from radreport.db.session import tenant_session

        with tenant_session(tenant_id, url=self.url) as session:
            row = session.execute(text("UPDATE llm_response_cache SET hits = hits + 1, last_hit_at = now() WHERE tenant_id = :t AND cache_key = :k AND expires_at > now() RETURNING response"), {"t": tenant_id, "k": key}).scalar_one_or_none()
        return row

    def put(self, tenant_id: uuid.UUID, key: str, value: dict[str, Any], *, model_id: str, task_key: str | None, ttl_hours: float) -> None:
        from sqlalchemy import text

        from radreport.db.session import tenant_session

        with tenant_session(tenant_id, url=self.url) as session:
            session.execute(text("INSERT INTO llm_response_cache (tenant_id, cache_key, model_id, task_key, response, expires_at) VALUES (:t, :k, :m, :task, CAST(:v AS jsonb), now() + make_interval(secs => :ttl)) ON CONFLICT (tenant_id, cache_key) DO UPDATE SET response = EXCLUDED.response, expires_at = EXCLUDED.expires_at"), {"t": tenant_id, "k": key, "m": model_id, "task": task_key, "v": json.dumps(value), "ttl": ttl_hours * 3600})
            if random.random() < PURGE_EVERY:
                # Expired replies are never served; sweep them now and then rather than on a schedule that would need every lab bound.
                session.execute(text("DELETE FROM llm_response_cache WHERE tenant_id = :t AND expires_at < now()"), {"t": tenant_id})


class SharedResponseStore:
    """The shared cache (Redis, or this worker's memory), with keys that carry the lab."""

    def get(self, tenant_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        from radreport.cache import shared
        from radreport.cache.keys import key as cache_key

        raw = shared.get_backend().get(cache_key("llm_response", tenant_id, key).render())
        return json.loads(raw) if raw else None

    def put(self, tenant_id: uuid.UUID, key: str, value: dict[str, Any], *, model_id: str, task_key: str | None, ttl_hours: float) -> None:
        from radreport.cache import shared
        from radreport.cache.keys import key as cache_key

        shared.get_backend().set(cache_key("llm_response", tenant_id, key).render(), json.dumps(value).encode(), ttl_hours * 3600)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    uncacheable: int = 0
    saved_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses, "uncacheable": self.uncacheable, "hit_rate": round(self.hits / total, 4) if total else 0.0, "saved_usd": round(self.saved_usd, 6)}


STATS = CacheStats()


class CachingLLMClient:
    """An LLMClient that answers repeats from the store. Batch calls pass straight through."""

    def __init__(self, inner: LLMClient, *, tenant_id: uuid.UUID, store: ResponseStore, ttl_hours: float, task_key: str | None = None) -> None:
        self.inner = inner
        self.provider_name = inner.provider_name
        self.tenant_id = tenant_id
        self.store = store
        self.ttl_hours = ttl_hours
        self.task_key = task_key

    async def complete(self, request: LLMRequest, *, model_id: str) -> LLMResponse:
        if not cacheable(request):
            with STATS._lock:
                STATS.uncacheable += 1
            return await self.inner.complete(request, model_id=model_id)
        key = request_fingerprint(request, model_id=model_id)
        try:
            found = await asyncio.to_thread(self.store.get, self.tenant_id, key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not fail a report
            log.warning("llm_response_cache_unavailable", op="get", error=type(exc).__name__)
            found = None
        if found is not None:
            reply = _decode(found)
            with STATS._lock:
                STATS.hits += 1
                STATS.saved_usd += float(found.get("cost_usd") or 0.0)
            log.info("llm_response_cache_hit", model_id=model_id, task_key=request.metadata.get("task_key", self.task_key), saved_usd=found.get("cost_usd"))
            return reply
        with STATS._lock:
            STATS.misses += 1
        reply = await self.inner.complete(request, model_id=model_id)
        try:
            await asyncio.to_thread(self.store.put, self.tenant_id, key, _encode(reply), model_id=model_id, task_key=request.metadata.get("task_key", self.task_key), ttl_hours=self.ttl_hours)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_response_cache_unavailable", op="put", error=type(exc).__name__)
        return reply

    async def submit_batch(self, items: list[BatchRequestItem], *, model_id: str) -> str:
        return await self.inner.submit_batch(items, model_id=model_id)

    async def poll_batch(self, batch_id: str) -> str:
        return await self.inner.poll_batch(batch_id)

    async def fetch_batch_results(self, batch_id: str) -> dict[str, LLMResponse]:
        return await self.inner.fetch_batch_results(batch_id)


def with_response_cache(client: LLMClient, *, tenant_id: uuid.UUID, url: str | None = None) -> LLMClient:
    """Wrap `client` as settings say; unchanged when the cache is off."""
    settings = get_settings().llm
    if settings.response_cache == "off":
        return client
    store: ResponseStore = PostgresResponseStore(url) if settings.response_cache == "postgres" else SharedResponseStore()
    return CachingLLMClient(client, tenant_id=tenant_id, store=store, ttl_hours=settings.response_cache_ttl_hours)
