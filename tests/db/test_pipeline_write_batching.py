"""A pipeline run writes its trace and its draft in a few batched flushes, not a round trip per row."""

from __future__ import annotations

import pytest

from radreport.core.types import PipelineTrigger, RunStatus
from radreport.db import instrumentation
from radreport.db.models.orchestration import StageExecution
from radreport.db.session import tenant_session
from radreport.pipeline.graph import new_run
from radreport.pipeline.v1 import V1_PIPELINE_VERSION
from tests.db.test_pipeline_v1 import _graph, pipeline_fixture  # noqa: F401 - the fixture is used by name

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_a_run_needs_few_statements(pipeline_fixture) -> None:  # noqa: F811
    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]
    with tenant_session(tenant_id, url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD, pipeline_version=V1_PIPELINE_VERSION)
        state.audio_object_key = fixture["object_key"]
        with instrumentation.query_scope("run", keep_statements=True) as stats:
            await _graph(fixture).run(state, ctx, session)
        assert run.status == RunStatus.SUCCEEDED
        executions = session.query(StageExecution).filter(StageExecution.pipeline_run_id == run.id).count()
    stage_inserts = [s for s in stats.statements or [] if s.startswith("INSERT INTO stage_execution")]
    print(f"{stats.count} statements for {executions} stages; {len(stage_inserts)} stage_execution INSERT statements")
    assert executions == 15
    assert len(stage_inserts) <= 2, "the stage trace is inserted as one batch"
    assert stats.count <= 12, "was 37 before the run buffered its writes"
