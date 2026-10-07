"""What each kind of job does. A handler gets a session already bound to the job's lab.

Order: register a handler under its kind (handler) -> look one up for a claimed job (get) ->
the shipped kinds: run_pipeline, ensure_partitions (registered by their own modules on import,
see load_all).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from radreport.workers.queue import ClaimedJob

Handler = Callable[[Session, ClaimedJob], Awaitable[dict[str, Any] | None]]
_HANDLERS: dict[str, Handler] = {}


class UnknownJobKind(LookupError):
    """No handler is registered for a claimed job's kind."""


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Register `fn` as the handler for `kind`."""

    def register(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return register


def get(kind: str) -> Handler:
    load_all()
    try:
        return _HANDLERS[kind]
    except KeyError as exc:
        raise UnknownJobKind(f"no handler for job kind {kind!r}") from exc


def kinds() -> list[str]:
    load_all()
    return sorted(_HANDLERS)


def load_all() -> None:
    """Import the modules that register the shipped handlers."""
    from radreport.pipeline import runner  # noqa: F401 - registers run_pipeline
    from radreport.workers import maintenance  # noqa: F401 - registers the platform jobs
