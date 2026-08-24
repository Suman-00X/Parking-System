"""Cost accounting across a run, and the hard spending cap that stops it."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.errors import BudgetExceeded
from radreport.pipeline.context import RunContext
from radreport.pipeline.state import PipelineState, Utterance


def _ctx(cap: float | None = None) -> RunContext:
    return RunContext(tenant_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), recording_id=uuid.uuid4(), budget_cap_usd=cap)


def test_cost_accumulates_per_stage() -> None:
    """Per-stage attribution is what lets the optimisation ladder be verified rather than asserted."""
    ctx = _ctx()
    ctx.record_cost(0.01, stage_name="extract")
    ctx.record_cost(0.02, stage_name="extract")
    ctx.record_cost(0.005, stage_name="verify")

    assert ctx.spent_usd == pytest.approx(0.035)
    assert ctx.cost_by_stage == {"extract": pytest.approx(0.03), "verify": pytest.approx(0.005)}


def test_budget_cap_aborts_hard() -> None:
    ctx = _ctx(cap=0.05)
    ctx.record_cost(0.04, stage_name="extract")
    with pytest.raises(BudgetExceeded) as exc:
        ctx.record_cost(0.03, stage_name="extract")
    assert exc.value.cap_usd == 0.05


def test_spend_is_recorded_even_on_the_call_that_trips_the_cap() -> None:
    """A stage that already paid a provider must have that recorded."""
    ctx = _ctx(cap=0.05)
    with pytest.raises(BudgetExceeded):
        ctx.record_cost(0.09, stage_name="extract")
    assert ctx.spent_usd == pytest.approx(0.09)
    assert ctx.cost_by_stage["extract"] == pytest.approx(0.09)


def test_no_cap_means_no_abort() -> None:
    ctx = _ctx(cap=None)
    ctx.record_cost(100.0, stage_name="extract")
    assert ctx.spent_usd == 100.0


async def test_resolving_a_model_without_a_resolver_is_an_error() -> None:
    """Every call site must ask the registry; none may name a model."""
    with pytest.raises(RuntimeError, match="TaskModelResolver"):
        await _ctx().resolve_model("extraction")


def test_log_context_carries_no_phi() -> None:
    ctx = _ctx()
    assert set(ctx.as_log_context()) == {"tenant_id", "pipeline_run_id", "recording_id", "is_shadow"}


# ----------------------------------------------------------------- state ----
def test_only_included_utterances_can_ground_a_field() -> None:
    """Invariant I2 plus the grounding rule."""
    state = PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), utterances=[Utterance(seq=0, char_start=0, char_end=15, audio_start_ms=0, audio_end_ms=1500, text="the left kidney", label="report_content", superseded_by_seq=1, is_included_downstream=False), Utterance(seq=1, char_start=16, char_end=38, audio_start_ms=1500, audio_end_ms=3000, text="sorry the right kidney", label="self_correction", is_included_downstream=True), Utterance(seq=2, char_start=39, char_end=60, audio_start_ms=3000, audio_end_ms=4000, text="can you close the door", label="aside", is_included_downstream=False)])

    included = state.included_utterances()
    assert [u.seq for u in included] == [1]
    # Nothing was deleted — the retracted span is still there to render
    # struck-through in the review UI.
    assert len(state.utterances) == 3
