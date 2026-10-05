"""Stage 17: files a report without a human reviewer, when stage 15 said it could.

Order: AutonomousReleaseStage acts on stage 15's decision; it refuses, rather than failing
quietly, if anything about that decision no longer holds.
"""

from __future__ import annotations

import datetime as dt
import uuid

from radreport.autonomy.release import AutonomyGrant
from radreport.core.hashing import canonical_json, hash_text
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, ExportStatus, PathType
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.review import FinalReport
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.state import PipelineState

log = get_logger(__name__)


class ReleaseWithoutReviewError(RuntimeError):
    """Stage 15 approved a release that could not be filed."""


class AutonomousReleaseStage:
    """Builds the `final_report`; the orchestrator writes it."""

    name = "autonomous_release"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider) -> None:
        self._knowledge = knowledge

    def is_idempotent(self) -> bool:
        """False, for `persist_draft`'s reason: re-running files a second report."""
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        routing = state.human_routing
        if routing is None or not routing.released_without_review:
            # The overwhelmingly common case, and not a warning: a report going
            # to a human is the system working as designed.
            return StageResult(output=state, confidence=1.0)

        grant = self._resolve_grant(state)
        report = _build_report(state, grant)

        log.warning("report_released_without_review", final_report_id=str(report.id), recording_id=str(state.recording_id), autonomy_class=grant.class_code, confidence=routing.confidence, signed_by=str(report.signed_by))
        return StageResult(
            output=state,
            confidence=1.0,
            # Surfaced on the run, not only in the log.
            warnings=[f"released without review: {routing.reason}"],
            pending_writes=[
                report,
                AuditLog(
                    tenant_id=state.tenant_id,
                    # SYSTEM with no actor: nobody decided *this* report.
                    actor_id=None,
                    actor_type=ActorType.SYSTEM,
                    action="report_released_without_review",
                    entity_type="final_report",
                    entity_id=report.id,
                    after={"draft_id": str(report.report_draft_id), "path_type": PathType.AUTONOMOUS, "autonomy_class": grant.class_code, "confidence": routing.confidence, "content_hash": report.content_hash, "signed_by": str(report.signed_by), "reason": routing.reason},
                ),
            ],
        )

    def _resolve_grant(self, state: PipelineState) -> AutonomyGrant:
        """Re-read the class from the same snapshot stage 15 decided against."""
        routing = state.human_routing
        assert routing is not None  # guarded by `run`

        version_id = state.routing.chosen_template_version_id if state.routing is not None else None
        grant = self._knowledge.for_tenant(state.tenant_id).autonomy.get(version_id) if version_id is not None else None
        if grant is None or grant.class_code != routing.autonomy_class_code:
            raise ReleaseWithoutReviewError(f"the autonomy class behind this release could not be resolved ({routing.autonomy_class_code!r} expected, got {grant.class_code if grant else None!r}); refusing to file a report whose authority cannot be recorded")
        return grant


def _build_report(state: PipelineState, grant: AutonomyGrant) -> FinalReport:
    """The `final_report` row. Every absence here is a NULL in a legal record."""
    if state.report_draft_id is None:
        raise ReleaseWithoutReviewError("no draft was persisted, so there is nothing to release; stage 16 must run before this one")
    if state.study_id is None:
        raise ReleaseWithoutReviewError("final_report.study_id is NOT NULL and the orchestrator did not resolve the recording's study onto state")
    if state.radiologist is None:
        raise ReleaseWithoutReviewError("a released report still needs an author of record, and no dictating radiologist was resolved onto state")

    text = state.rendered_text or ""
    payload = {key: {"value_text": value.value_text, "value_enum": value.value_enum, "value_numeric": value.value_numeric, "value_unit": value.value_unit, "assertion_status": value.assertion_status, "laterality": value.laterality, "fill_source": value.fill_source, "is_grounded": value.is_grounded} for key, value in state.field_values.items()}

    return FinalReport(
        id=uuid.uuid4(),
        tenant_id=state.tenant_id,
        study_id=state.study_id,
        report_draft_id=state.report_draft_id,
        # The null migration 0006 exists for.
        final_revision_id=None,
        # The dictating radiologist: the author of the content and the only person with standing to be named on it.
        signed_by=state.radiologist.user_id,
        signed_at=dt.datetime.now(dt.UTC),
        rendered_text=text,
        structured_payload=payload,
        # Hashed exactly as `signing.sign_report` does, so tamper evidence is
        # comparable across paths rather than per-path.
        content_hash=hash_text(canonical_json({"text": text, "fields": payload})),
        path_type=PathType.AUTONOMOUS,
        autonomy_class_id=grant.class_id,
        export_status=ExportStatus.PENDING,
    )
