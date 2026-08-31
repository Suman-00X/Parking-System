"""Stage 16: writes the finished draft and everything behind it to the database in one go.

Order: PersistDraftStage saves the draft, its field values, provenance and findings together, so
a failure leaves nothing half-written.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from radreport.core.logging import get_logger
from radreport.core.types import DraftStatus, TranscriptStage
from radreport.db.models.asr import Transcript, TranscriptUtterance
from radreport.db.models.reporting import ProvenanceSpan, ReportDraft, ReportFieldValue, VerificationFinding
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.state import PipelineState

log = get_logger(__name__)


@dataclass(slots=True)
class PersistResult:
    draft_id: uuid.UUID | None = None
    transcript_id: uuid.UUID | None = None
    field_values: int = 0
    provenance_spans: int = 0
    utterances: int = 0
    skipped_fields: list[str] = field(default_factory=list)
    """Values whose `field_key` has no `template_field` under the routed version."""


class PersistDraftStage:
    """Builds domain rows; the orchestrator writes them."""

    name = "persist_draft"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider, *, prompt_bundle_version: str = "v1") -> None:
        self._knowledge = knowledge
        self._prompt_bundle_version = prompt_bundle_version

    def is_idempotent(self) -> bool:
        """False: re-running writes a second draft for the same recording."""
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("nothing to persist: the run produced no transcript")
        if state.routing is None or state.routing.chosen_template_version_id is None:
            # Not a failure.
            log.warning("persist_skipped_no_template", recording_id=str(state.recording_id), detail="routing declined; transcript persisted, no draft created")
            return StageResult(output=state, confidence=0.0, warnings=["no template was chosen, so no draft was created"], pending_writes=list(self._transcript_rows(state)[1]))

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        version_id = state.routing.chosen_template_version_id
        field_ids = knowledge.template_fields.get(version_id, {})

        transcript, transcript_rows = self._transcript_rows(state)
        result = PersistResult(transcript_id=transcript.id, utterances=len(transcript_rows) - 1)
        pending: list[object] = list(transcript_rows)

        flagged = sum(1 for v in state.field_values.values() if v.is_flagged)
        # An autonomously released draft is born `signed`, set here because the review queue selects on this column.
        released = bool(state.human_routing is not None and state.human_routing.released_without_review)
        draft = ReportDraft(
            id=uuid.uuid4(),
            tenant_id=state.tenant_id,
            recording_id=state.recording_id,
            template_version_id=version_id,
            pipeline_run_id=state.pipeline_run_id,
            rendered_text=state.rendered_text or "",
            structured_payload=_payload(state),
            overall_confidence=state.overall_confidence or 0.0,
            flagged_field_count=flagged,
            prompt_bundle_version=self._prompt_bundle_version,
            # a vendor-side model change is a pipeline change, and this
            # is where you prove which model produced a given draft.
            model_versions={ref.engine: ref.engine_version for ref in state.asr_runs},
            status=DraftStatus.SIGNED if released else DraftStatus.GENERATED,
        )
        pending.append(draft)
        result.draft_id = draft.id
        # Stage 17 needs it and has no session to look it up with. The row is
        # still uncommitted — this is the id, not the row.
        state.report_draft_id = draft.id

        # Keyed off each row's own `seq`, not by zipping two lists.
        utterance_ids = {row.seq: row.id for row in transcript_rows if isinstance(row, TranscriptUtterance)}

        for field_key, value in state.field_values.items():
            template_field_id = field_ids.get(field_key)
            if template_field_id is None:
                result.skipped_fields.append(field_key)
                continue

            row = ReportFieldValue(
                id=uuid.uuid4(),
                tenant_id=state.tenant_id,
                report_draft_id=draft.id,
                template_field_id=template_field_id,
                value_text=value.value_text,
                value_numeric=value.value_numeric,
                value_unit=value.value_unit,
                value_enum=value.value_enum,
                assertion_status=value.assertion_status,
                laterality=value.laterality,
                fill_source=value.fill_source,
                # Written even when false: requires the row either to carry a span or to say it has none, and a value the model produced that nobody can see is a value nobody can correct.
                is_grounded=value.is_grounded,
                confidence=value.confidence,
                is_flagged=value.is_flagged,
                flag_reasons=list(value.flag_reasons) or None,
            )
            pending.append(row)
            result.field_values += 1

            for ref in value.provenance:
                pending.append(ProvenanceSpan(tenant_id=state.tenant_id, report_field_value_id=row.id, transcript_id=transcript.id, utterance_id=utterance_ids.get(ref.utterance_seq) if ref.utterance_seq is not None else None, char_start=ref.char_start, char_end=ref.char_end, audio_start_ms=ref.audio_start_ms, audio_end_ms=ref.audio_end_ms, extraction_confidence=ref.extraction_confidence))
                result.provenance_spans += 1

        for finding in state.verification:
            pending.append(VerificationFinding(tenant_id=state.tenant_id, report_draft_id=draft.id, check_id=finding.check_id, check_type=finding.check_type, severity=finding.severity, message=finding.message, field_key=finding.field_key, evidence=finding.evidence))

        warnings: list[str] = []
        if result.skipped_fields:
            warnings.append(f"{len(result.skipped_fields)} extracted field(s) have no template_field under the routed version: {', '.join(sorted(result.skipped_fields)[:5])}")

        log.info("draft_persisted", draft_id=str(draft.id), field_values=result.field_values, provenance_spans=result.provenance_spans, utterances=result.utterances, flagged=flagged, skipped_fields=len(result.skipped_fields))
        return StageResult(output=state, confidence=1.0, warnings=warnings, pending_writes=pending)

    def _transcript_rows(self, state: PipelineState) -> tuple[Transcript, list[object]]:
        """The transcript and its utterances, with the retraction links resolved."""
        assert state.transcript is not None
        transcript = Transcript(id=uuid.uuid4(), tenant_id=state.tenant_id, recording_id=state.recording_id, version=state.transcript.version, stage=state.transcript.stage or TranscriptStage.RAW, text=state.transcript.text, source_asr_run_ids=[ref.asr_run_id for ref in state.asr_runs], reconciliation_method=state.transcript.reconciliation_method, disagreement_score=state.transcript.disagreement_score, is_current=True)
        rows: list[object] = [transcript]

        ordered = sorted(state.utterances, key=lambda u: u.seq)
        by_seq: dict[int, TranscriptUtterance] = {}
        for utterance in ordered:
            row = TranscriptUtterance(id=uuid.uuid4(), tenant_id=state.tenant_id, transcript_id=transcript.id, seq=utterance.seq, char_start=utterance.char_start, char_end=utterance.char_end, audio_start_ms=utterance.audio_start_ms, audio_end_ms=utterance.audio_end_ms, text=utterance.text, label=utterance.label, label_confidence=utterance.label_confidence, label_source=utterance.label_source, contains_clinical_tokens=utterance.contains_clinical_tokens, is_included_downstream=utterance.is_included_downstream)
            by_seq[utterance.seq] = row
            rows.append(row)

        # Second pass: resolve the retraction links. Nothing is deleted — the
        # retracted span is marked and renders struck-through (I2).
        supersedes: dict[int, int] = {}
        for utterance in ordered:
            if utterance.superseded_by_seq is None:
                continue
            target = by_seq.get(utterance.superseded_by_seq)
            if target is not None and utterance.superseded_by_seq != utterance.seq:
                by_seq[utterance.seq].superseded_by_id = target.id
                supersedes[utterance.seq] = utterance.superseded_by_seq

        # `superseded_by_id` is a self-referential FK, so the row it points at must be inserted first — and a retraction always comes *before* its correction in sequence order, which is exactly the wrong order.
        rows = [transcript, *_in_reference_order(ordered, by_seq, supersedes)]
        return transcript, rows


def _in_reference_order(ordered: list, by_seq: dict[int, TranscriptUtterance], supersedes: dict[int, int]) -> list[TranscriptUtterance]:
    """Utterance rows with every supersede target ahead of its referrer."""
    emitted: list[TranscriptUtterance] = []
    done: set[int] = set()
    visiting: set[int] = set()

    def emit(seq: int) -> None:
        if seq in done or seq in visiting:
            return
        visiting.add(seq)
        target = supersedes.get(seq)
        if target is not None:
            emit(target)
        visiting.discard(seq)
        if seq not in done:
            done.add(seq)
            emitted.append(by_seq[seq])

    for utterance in ordered:
        emit(utterance.seq)
    return emitted


def _payload(state: PipelineState) -> dict[str, object]:
    return {key: {"value_text": value.value_text, "value_enum": value.value_enum, "value_numeric": value.value_numeric, "value_unit": value.value_unit, "assertion_status": value.assertion_status, "laterality": value.laterality, "fill_source": value.fill_source, "is_grounded": value.is_grounded} for key, value in state.field_values.items()}
