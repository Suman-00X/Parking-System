"""The contract every stage obeys: take the state, return a new state plus a confidence and any warnings.

Defines: Stage, the protocol a stage implements, StageContext, what it is given, and
StageResult, what it must return.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


class StageResult[TOut: BaseModel](BaseModel):
    """What a stage hands back. Never a domain-table write."""

    output: TOut
    confidence: float = Field(ge=0.0, le=1.0)
    cost_usd: float = 0.0
    duration_ms: int = 0

    model_id: str | None = None
    """The **resolved** provider identifier (: no date suffix)."""

    model_version: str | None = None
    prompt_version: str | None = None

    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read_tokens: int | None = None
    """From `usage.cache_read_input_tokens`."""

    cache_write_tokens: int | None = None

    warnings: list[str] = Field(default_factory=list)

    #: Domain rows the orchestrator should commit on the stage's behalf.
    #: Deliberately opaque here — `pipeline/graph.py` owns the writing.
    pending_writes: list[Any] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class StageContext(Protocol):
    """What a stage may touch. Notably not a database session."""

    tenant_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    recording_id: uuid.UUID
    is_shadow: bool

    def record_cost(self, usd: float) -> None:
        """Accumulate against `pipeline_run.total_cost_usd`."""
        ...

    async def resolve_model(self, task_key: str) -> Any:
        """ "Which model serves task `extraction` **for tenant T**?"."""
        ...


class Stage[TIn: BaseModel, TOut: BaseModel](Protocol):
    """The contract a stage implements: take the state, return a new state with a confidence."""

    name: str
    version: str

    async def run(self, inp: TIn, ctx: StageContext) -> StageResult[TOut]: ...

    def is_idempotent(self) -> bool:
        """True ⇒ safe to replay without re-billing or double-writing."""
        ...
