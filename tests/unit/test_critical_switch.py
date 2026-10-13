"""The per-lab critical-findings switch: off, a report is passed through with a warning and no alert; on, rules fire."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import AlertSeverity, PatternType
from radreport.pipeline.stages.critical import CriticalFindingsStage
from radreport.pipeline.stages.providers import CriticalRuleEntry, StaticKnowledgeProvider, TenantKnowledge
from radreport.pipeline.state import PipelineState, TranscriptState

_RULE = CriticalRuleEntry(rule_id=uuid.uuid4(), rule_code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern_type=PatternType.LEXICAL, patterns=("pneumothorax",), negation_sensitive=True, severity=AlertSeverity.RED, sla_minutes=60)


async def _run(*, alerts_on: bool):
    tenant_id = uuid.uuid4()
    stage = CriticalFindingsStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=tenant_id, critical_rules=(_RULE,), critical_alerts_on=alerts_on)))
    state = PipelineState(tenant_id=tenant_id, recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text="There is a large right pneumothorax."))
    return await stage.run(state, None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_switched_off_raises_nothing_and_says_so() -> None:
    result = await _run(alerts_on=False)
    assert result.output.critical_alerts == [] and not result.pending_writes
    assert any("switched off" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_switched_on_the_same_report_raises_its_alert() -> None:
    result = await _run(alerts_on=True)
    assert [a.rule_code for a in result.output.critical_alerts] == ["PNEUMOTHORAX"]
    assert len(result.pending_writes) == 1
