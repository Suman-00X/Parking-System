"""Opening a draft, editing it and recording exactly what changed -- the record every later improvement is built on.

Order: open the draft for a reviewer (open_draft, field_sort_key) -> classify each change
(categorise_edit) -> save it as a new version with its individual edits (record_revision).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ActorType, DraftStatus, EditType, ErrorCategory, FillSource, ReviewerRole
from radreport.db.models.asr import Transcript, TranscriptUtterance
from radreport.db.models.knowledge import TemplateField
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import ProvenanceSpan, ReportDraft, ReportFieldValue, VerificationFinding
from radreport.db.models.review import EditEvent, ReportRevision
from radreport.knowledge.phonetics import phonetic_distance
from radreport.review.rbac import Permission, Reviewer, require

log = get_logger(__name__)

_LATERALITY = re.compile(r"\b(left|right|bilateral|midline)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(no|not|without|absent|negative|denies|free of)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: Below this phonetic distance, a changed term is a mishearing rather than a
#: rewording — which is what makes it an ASR training example rather than style.
ASR_TERM_MAX_DISTANCE = 0.34


@dataclass(frozen=True, slots=True)
class FieldView:
    """One field as the review screen shows it."""

    field_value_id: uuid.UUID
    field_key: str
    display_label: str
    section: str
    seq: int
    value_text: str | None
    value_numeric: float | None
    value_unit: str | None
    value_enum: str | None
    assertion_status: str
    laterality: str | None
    fill_source: str
    is_grounded: bool
    is_flagged: bool
    is_critical: bool
    confidence: float
    flag_reasons: tuple[str, ...]
    provenance: tuple[dict[str, object], ...]
    """Each carries `audio_start_ms`/`audio_end_ms` — this is what makes per-field click-to-listen possible."""

    default_normal_text: str | None
    """Shown beside the field whenever a default filled it, so the phrase itself is visible and not merely the fact of a default."""

    @property
    def system_asserted(self) -> bool:
        """`fill_source != 'dictated'` — the system claiming something no human said."""
        return self.fill_source != FillSource.DICTATED

    @property
    def renders(self) -> bool:
        """Ungrounded fields never render (I1)."""
        return self.is_grounded


@dataclass(frozen=True, slots=True)
class Retraction:
    """A span the radiologist took back, and what replaced it."""

    seq: int
    text: str
    superseded_by_seq: int | None
    superseded_by_text: str | None
    audio_start_ms: int
    audio_end_ms: int


@dataclass(slots=True)
class DraftView:
    draft_id: uuid.UUID
    recording_id: uuid.UUID
    status: str
    rendered_text: str
    overall_confidence: float
    fields: list[FieldView] = field(default_factory=list)
    findings: list[dict[str, object]] = field(default_factory=list)
    retractions: list[Retraction] = field(default_factory=list)
    opened_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def system_asserted_fields(self) -> list[FieldView]:
        return [f for f in self.fields if f.system_asserted]


def field_sort_key(view: FieldView) -> tuple[int, int, int, str]:
    """Flagged first, then ungrounded, then template order."""
    return (0 if view.is_flagged else 1, 0 if view.is_critical else 1, view.seq, view.field_key)


def open_draft(session: Session, *, tenant_id: uuid.UUID, draft_id: uuid.UUID, reviewer: Reviewer) -> DraftView:
    """Load a draft for review and mark it in progress."""
    require(reviewer, Permission.VIEW_DRAFT)

    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no report_draft {draft_id} in this tenant")

    rows = session.execute(select(ReportFieldValue, TemplateField).join(TemplateField, TemplateField.id == ReportFieldValue.template_field_id).where(ReportFieldValue.tenant_id == tenant_id, ReportFieldValue.report_draft_id == draft_id)).all()

    spans_by_value: dict[uuid.UUID, list[dict[str, object]]] = {}
    for span in session.execute(select(ProvenanceSpan).where(ProvenanceSpan.tenant_id == tenant_id, ProvenanceSpan.report_field_value_id.in_([v.id for v, _ in rows] or [None]))).scalars().all():
        spans_by_value.setdefault(span.report_field_value_id, []).append({"char_start": span.char_start, "char_end": span.char_end, "audio_start_ms": span.audio_start_ms, "audio_end_ms": span.audio_end_ms})

    fields = [
        FieldView(
            field_value_id=value.id,
            field_key=template_field.field_key,
            display_label=template_field.display_label,
            section=template_field.section,
            seq=template_field.seq,
            value_text=value.value_text,
            value_numeric=float(value.value_numeric) if value.value_numeric is not None else None,
            value_unit=value.value_unit,
            value_enum=value.value_enum,
            assertion_status=value.assertion_status,
            laterality=value.laterality,
            fill_source=value.fill_source,
            is_grounded=value.is_grounded,
            is_flagged=value.is_flagged,
            is_critical=template_field.is_critical,
            confidence=float(value.confidence),
            flag_reasons=tuple(value.flag_reasons or ()),
            provenance=tuple(spans_by_value.get(value.id, ())),
            default_normal_text=(template_field.default_normal_text if value.fill_source in (FillSource.TEMPLATE_DEFAULT, FillSource.BLANKET_NORMAL) else None),
        )
        for value, template_field in rows
    ]
    fields.sort(key=field_sort_key)

    # A finding points at a field value, not a field key; resolve it through the fields just loaded.
    key_by_value = {view.field_value_id: view.field_key for view in fields}
    findings = [{"check_id": f.check_id, "severity": f.severity, "message": f.message, "field_key": key_by_value.get(f.report_field_value_id) if f.report_field_value_id else None} for f in session.execute(select(VerificationFinding).where(VerificationFinding.tenant_id == tenant_id, VerificationFinding.report_draft_id == draft_id)).scalars().all()]

    if draft.status == DraftStatus.GENERATED:
        draft.status = DraftStatus.IN_REVIEW
        session.flush()

    return DraftView(retractions=_retractions(session, tenant_id, draft.recording_id), draft_id=draft.id, recording_id=draft.recording_id, status=draft.status, rendered_text=draft.rendered_text, overall_confidence=float(draft.overall_confidence), fields=fields, findings=findings)


def _retractions(session: Session, tenant_id: uuid.UUID, recording_id: uuid.UUID) -> list[Retraction]:
    """Self-corrections from the current transcript, with their overrides."""
    transcript = session.execute(select(Transcript).where(Transcript.tenant_id == tenant_id, Transcript.recording_id == recording_id, Transcript.is_current.is_(True)).order_by(Transcript.version.desc())).scalars().first()
    if transcript is None:
        return []

    utterances = list(session.execute(select(TranscriptUtterance).where(TranscriptUtterance.tenant_id == tenant_id, TranscriptUtterance.transcript_id == transcript.id)).scalars().all())
    by_id = {u.id: u for u in utterances}

    return [Retraction(seq=u.seq, text=u.text, superseded_by_seq=(by_id[u.superseded_by_id].seq if u.superseded_by_id in by_id else None), superseded_by_text=(by_id[u.superseded_by_id].text if u.superseded_by_id in by_id else None), audio_start_ms=u.audio_start_ms, audio_end_ms=u.audio_end_ms) for u in sorted(utterances, key=lambda x: x.seq) if u.superseded_by_id is not None]


@dataclass(frozen=True, slots=True)
class FieldEdit:
    """One field the reviewer changed."""

    field_value_id: uuid.UUID
    value_text: str | None = None
    value_numeric: float | None = None
    value_unit: str | None = None
    value_enum: str | None = None
    assertion_status: str | None = None
    laterality: str | None = None


@dataclass(slots=True)
class RevisionResult:
    revision: ReportRevision
    edit_events: list[EditEvent] = field(default_factory=list)
    active_edit_seconds: int = 0
    clamped: bool = False
    """True when the reported focus time exceeded the wall clock and was reduced."""


def categorise_edit(before: str | None, after: str | None) -> str:
    """Classify one field edit into the error taxonomy."""
    before_text = (before or "").strip()
    after_text = (after or "").strip()

    if before_text and not after_text:
        # The reviewer deleted what the system asserted.
        return ErrorCategory.HALLUCINATION
    if after_text and not before_text:
        return ErrorCategory.EXTRACTION_MISS

    before_lat = {m.group(0).lower() for m in _LATERALITY.finditer(before_text)}
    after_lat = {m.group(0).lower() for m in _LATERALITY.finditer(after_text)}
    if before_lat != after_lat:
        return ErrorCategory.LATERALITY

    if bool(_NEGATION.search(before_text)) != bool(_NEGATION.search(after_text)):
        return ErrorCategory.NEGATION

    if set(_NUMBER.findall(before_text)) != set(_NUMBER.findall(after_text)):
        return ErrorCategory.ASR_NUMBER

    # A near-homophone replacement is a mishearing, not a rewording — which is
    # exactly what makes it an ASR training example.
    if before_text and after_text and phonetic_distance(before_text, after_text) <= (ASR_TERM_MAX_DISTANCE):
        return ErrorCategory.ASR_TERM

    return ErrorCategory.STYLE


def record_revision(session: Session, *, tenant_id: uuid.UUID, draft_id: uuid.UUID, reviewer: Reviewer, edits: list[FieldEdit], active_edit_seconds: int, wall_clock_seconds: int, rendered_text: str, is_eval_item: bool = False) -> RevisionResult:
    """Apply a reviewer's edits and record the revision and its diffs."""
    require(reviewer, Permission.REVISE_DRAFT)

    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no report_draft {draft_id} in this tenant")

    clamped = active_edit_seconds > wall_clock_seconds
    if clamped:
        log.warning("active_edit_seconds_clamped", draft_id=str(draft_id), reported=active_edit_seconds, wall_clock=wall_clock_seconds, detail="focus time exceeded elapsed time; check the review-screen timer")
    effective_seconds = max(0, min(active_edit_seconds, wall_clock_seconds))

    previous = session.execute(select(func.count()).select_from(ReportRevision).where(ReportRevision.tenant_id == tenant_id, ReportRevision.report_draft_id == draft_id)).scalar_one()

    revision = ReportRevision(id=uuid.uuid4(), tenant_id=tenant_id, report_draft_id=draft_id, revision_number=previous + 1, revised_by=reviewer.user_id, reviewer_role=(ReviewerRole.RADIOLOGIST if reviewer.is_radiologist else ReviewerRole.TRANSCRIPTIONIST), structured_payload={}, rendered_text=rendered_text, edit_distance_from_prev=0, active_edit_seconds=effective_seconds)
    session.add(revision)
    session.flush()

    result = RevisionResult(revision=revision, active_edit_seconds=effective_seconds, clamped=clamped)
    distance = 0

    for edit in edits:
        value = session.get(ReportFieldValue, edit.field_value_id)
        if value is None or value.tenant_id != tenant_id:
            continue

        before = _as_text(value)
        _apply(value, edit)
        after = _as_text(value)
        if before == after:
            continue

        distance += _edit_distance(before or "", after or "")
        span = session.execute(select(ProvenanceSpan).where(ProvenanceSpan.tenant_id == tenant_id, ProvenanceSpan.report_field_value_id == value.id).limit(1)).scalar_one_or_none()

        event = EditEvent(
            tenant_id=tenant_id,
            report_revision_id=revision.id,
            report_field_value_id=value.id,
            edit_type=_edit_type(before, after),
            before_value=before,
            after_value=after,
            # The audio span is what makes this row a training example rather
            # than a note that something changed.
            audio_start_ms=span.audio_start_ms if span else None,
            audio_end_ms=span.audio_end_ms if span else None,
            transcript_char_start=span.char_start if span else None,
            transcript_char_end=span.char_end if span else None,
            error_category=categorise_edit(before, after),
            # An eval-set item never becomes training data, and the
            # exclusion is written at the moment the row is created.
            is_training_eligible=not is_eval_item,
        )
        session.add(event)
        result.edit_events.append(event)

        # A reviewer's correction is dictated ground truth from here on.
        value.fill_source = FillSource.HUMAN
        value.is_flagged = False

    revision.edit_distance_from_prev = distance
    draft.status = DraftStatus.REVISED
    draft.flagged_field_count = session.execute(select(func.count()).select_from(ReportFieldValue).where(ReportFieldValue.tenant_id == tenant_id, ReportFieldValue.report_draft_id == draft_id, ReportFieldValue.is_flagged.is_(True))).scalar_one()

    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer.user_id, actor_type=ActorType.USER, action="report_revised", entity_type="report_draft", entity_id=draft_id, after={"revision_number": revision.revision_number, "edits": len(result.edit_events), "active_edit_seconds": effective_seconds, "reviewer_role": revision.reviewer_role}))
    session.flush()

    log.info("revision_recorded", draft_id=str(draft_id), revision_number=revision.revision_number, edits=len(result.edit_events), active_edit_seconds=effective_seconds, clamped=clamped, categories=[e.error_category for e in result.edit_events])
    return result


def _as_text(value: ReportFieldValue) -> str | None:
    if value.value_text:
        return value.value_text
    if value.value_enum:
        return value.value_enum
    if value.value_numeric is not None:
        unit = f" {value.value_unit}" if value.value_unit else ""
        return f"{float(value.value_numeric):g}{unit}"
    return None


def _apply(value: ReportFieldValue, edit: FieldEdit) -> None:
    if edit.value_text is not None:
        value.value_text = edit.value_text or None
    if edit.value_numeric is not None:
        value.value_numeric = edit.value_numeric
    if edit.value_unit is not None:
        value.value_unit = edit.value_unit or None
    if edit.value_enum is not None:
        value.value_enum = edit.value_enum or None
    if edit.assertion_status is not None:
        value.assertion_status = edit.assertion_status
    if edit.laterality is not None:
        value.laterality = edit.laterality


def _edit_type(before: str | None, after: str | None) -> str:
    if not before and after:
        return EditType.FIELD_ADDED
    if before and not after:
        return EditType.FIELD_REMOVED
    return EditType.VALUE_CHANGE


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein, for `edit_distance_from_prev`."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]
