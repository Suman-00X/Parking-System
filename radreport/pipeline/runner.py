"""Runs the pipeline for one recording from what the database holds, as the run_pipeline job.

Order: read the lab's routable templates (load_template_candidates) -> assemble the graph with the
configured engines (default_graph_factory, replaceable with set_graph_factory) -> create the run,
execute it, and report its outcome (run_recording, run_pipeline_job). A failed stage is recorded
on its pipeline_run and the job still completes: retrying the same input reproduces the same
failure, and the run row is what a person reviews.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.adapters.llm.registry import TaskModelResolver
from radreport.adapters.storage.object_store import ObjectStore, S3ObjectStore
from radreport.core.config import get_settings
from radreport.core.errors import BudgetExceeded, StageFailed
from radreport.core.logging import get_logger
from radreport.core.types import PathType, PipelineTrigger
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.reporting import CriticalFindingAlert, ReportDraft
from radreport.db.models.review import FinalReport
from radreport.events.outbox import Topic, emit
from radreport.pipeline.graph import PipelineGraph, new_run
from radreport.pipeline.stages.providers import StaticKnowledgeProvider, load_tenant_knowledge
from radreport.pipeline.stages.routing import TemplateCandidate
from radreport.pipeline.v1 import V1_PIPELINE_VERSION, build_v1_graph
from radreport.workers.handlers import handler
from radreport.workers.queue import ClaimedJob

log = get_logger(__name__)

GraphFactory = Callable[[Session, uuid.UUID], tuple[PipelineGraph, ObjectStore]]


def load_template_candidates(session: Session, tenant_id: uuid.UUID) -> list[TemplateCandidate]:
    """Every current, active template version the router may choose for this lab."""
    rows = session.execute(select(TemplateVersion, Template).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True), Template.is_active.is_(True))).all()
    return [TemplateCandidate(template_version_id=v.id, template_code=t.code, display_name=t.display_name, modality=t.modality, body_region=t.body_region, routing_card=v.routing_card, spoken_study_code=v.spoken_study_code, usage_count_12m=t.usage_count_12m, applicable_sex=tuple(t.applicable_sex or ()), applicable_age_min=t.applicable_age_min, applicable_age_max=t.applicable_age_max, is_module=t.is_module) for v, t in rows]


def _asr_engine() -> Any:
    from radreport.adapters.asr.whisper_local import StubASREngine, WhisperLocalEngine

    settings = get_settings().asr
    # The stub is the default until real audio and a model are on the machine; set RADREPORT_ASR__ENGINE_VERSION to use Whisper.
    return StubASREngine() if settings.engine_version.endswith("stub") else WhisperLocalEngine()


def default_graph_factory(session: Session, tenant_id: uuid.UUID) -> tuple[PipelineGraph, ObjectStore]:
    """The deterministic V1 graph from this lab's own knowledge, templates and the configured engines."""
    store = S3ObjectStore(get_settings().storage)
    knowledge = StaticKnowledgeProvider(load_tenant_knowledge(session, tenant_id))
    graph = build_v1_graph(store=store, asr_engine=_asr_engine(), knowledge=knowledge, templates=load_template_candidates(session, tenant_id), sections=[])
    return graph, store


_factory: GraphFactory = default_graph_factory


def set_graph_factory(factory: GraphFactory | None) -> None:
    """Replace how the graph is built (tests, or a deployment with model-backed stages); None restores the default."""
    global _factory
    _factory = factory or default_graph_factory


async def run_recording(session: Session, *, tenant_id: uuid.UUID, recording_id: uuid.UUID, trigger: str = PipelineTrigger.UPLOAD) -> dict[str, Any]:
    """Run the pipeline over one recording in `session`'s transaction."""
    recording = session.get(Recording, recording_id)
    if recording is None or recording.tenant_id != tenant_id:
        raise LookupError(f"no recording {recording_id} in this lab")
    graph, _store = _factory(session, tenant_id)
    run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=recording_id, trigger=trigger, budget_cap_usd=get_settings().llm.per_run_budget_usd, pipeline_version=graph.version if graph.version != "0.1.0-phase0" else V1_PIPELINE_VERSION)
    ctx.resolver = TaskModelResolver(session)
    ctx.radiologist_id = recording.radiologist_id
    state.audio_object_key = recording.object_key
    try:
        await graph.run(state, ctx, session)
    except (StageFailed, BudgetExceeded) as exc:
        log.warning("pipeline_run_failed", pipeline_run_id=str(run.id), recording_id=str(recording_id), error=str(exc)[:300])
    else:
        _announce(session, tenant_id=tenant_id, recording_id=recording_id, pipeline_run_id=run.id)
    return {"pipeline_run_id": str(run.id), "status": run.status, "cost_usd": float(run.total_cost_usd or 0)}


def _announce(session: Session, *, tenant_id: uuid.UUID, recording_id: uuid.UUID, pipeline_run_id: uuid.UUID) -> None:
    """Emit draft.ready for the run's draft, and report.signed when stage 17 released it without review."""
    draft = session.execute(select(ReportDraft).where(ReportDraft.tenant_id == tenant_id, ReportDraft.pipeline_run_id == pipeline_run_id)).scalars().first()
    if draft is None:
        return
    alerts = session.execute(select(func.count()).select_from(CriticalFindingAlert).where(CriticalFindingAlert.tenant_id == tenant_id, CriticalFindingAlert.recording_id == recording_id)).scalar_one()
    emit(session, Topic.DRAFT_READY, {"draft_id": draft.id, "recording_id": recording_id, "pipeline_run_id": pipeline_run_id, "flagged_fields": draft.flagged_field_count, "critical_alerts": int(alerts)}, tenant_id=tenant_id)
    released = session.execute(select(FinalReport).where(FinalReport.tenant_id == tenant_id, FinalReport.report_draft_id == draft.id, FinalReport.path_type == PathType.AUTONOMOUS)).scalars().first()
    if released is not None:
        emit(session, Topic.REPORT_SIGNED, {"report_id": released.id, "draft_id": draft.id, "path_type": released.path_type}, tenant_id=tenant_id)


@handler("run_pipeline")
async def run_pipeline_job(session: Session, job: ClaimedJob) -> dict[str, Any]:
    """The job ingest queues for every accepted recording."""
    assert job.tenant_id is not None, "run_pipeline is always a lab's job"
    return await run_recording(session, tenant_id=job.tenant_id, recording_id=uuid.UUID(job.payload["recording_id"]))
