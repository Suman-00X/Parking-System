"""Keeps the system from overwhelming a provider, and stops calling one that is failing.

Defines: ProviderLimiter, which caps how many calls run at once, and CircuitBreaker, which
opens after repeated failures and half-opens after a cooldown.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from radreport.core.errors import ProviderError, ProviderSaturated
from radreport.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CircuitBreaker:
    """Opens after repeated failures; half-opens after a cooldown."""

    failure_threshold: int = 5
    reset_seconds: float = 30.0

    _failures: int = 0
    _opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            # Half-open: let one call through to test the water.
            self._opened_at = None
            self._failures = self.failure_threshold - 1
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
            log.warning("circuit_open", failures=self._failures)

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


@dataclass
class ProviderLimiter:
    """One per provider. Bounds in-flight calls and retries with jittered backoff."""

    name: str
    max_concurrency: int = 8
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    _semaphore: asyncio.Semaphore | None = None

    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    async def call[T](self, fn, *args, **kwargs) -> T:  # noqa: ANN001
        """Run `fn` under the limiter, retrying transient failures."""
        if not self.breaker.allow():
            raise ProviderSaturated(f"{self.name}: circuit open")

        last: Exception | None = None
        async with self._sem():
            for attempt in range(1, self.max_attempts + 1):
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - classified below
                    last = exc
                    if not _is_retryable(exc) or attempt == self.max_attempts:
                        self.breaker.record_failure()
                        raise
                    delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
                    await asyncio.sleep(random.uniform(0, delay))
                else:
                    self.breaker.record_success()
                    return result

        self.breaker.record_failure()
        raise ProviderError(f"{self.name}: exhausted retries") from last


_RETRYABLE_MARKERS = ("rate_limit", "overloaded", "429", "500", "502", "503", "504", "timeout", "connection")


def _is_retryable(exc: Exception) -> bool:
    """Classify by message because provider SDKs disagree on exception types."""
    if isinstance(exc, ProviderSaturated):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)
