"""Finds the normal statements a lab writes most often, so they can be offered as defaults.

Order: mine candidate phrases from the corpus (mine_boilerplate) -> rank them by how much of
the corpus they cover (rank_candidates) -> export for review (export_candidates_csv) ->
accept (promote_candidate) or discard (reject_candidate).
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import AbsencePolicy, ActorType, ReviewStatus
from radreport.db.models.knowledge import Template, TemplateField, TemplateVersion
from radreport.db.models.onboarding import BoilerplateCandidate, CorpusReport, CorpusReportTemplateMap
from radreport.db.models.orchestration import AuditLog

log = get_logger(__name__)

#: presents "the top ~40" to the radiologist. Ranked by share, so a
#: statement used in 3% of reports never reaches the list.
TOP_N_FOR_REVIEW = 40

#: Below this share a statement is one radiologist's habit, not house style.
MIN_CORPUS_SHARE = 0.20

_FIELD_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /()'-]{1,60}?)\s*:\s*(.+?)\s*$", re.MULTILINE)
#: "Normal." / "No abnormality detected." — statements that assert absence.
_NORMAL_HINT = re.compile(
    r"\b(normal|unremarkable|no (?:significant )?abnormalit|within normal limits|"
    r"not (?:enlarged|dilated)|no evidence of)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class BoilerplateMiningResult:
    candidates_written: int = 0
    fields_scanned: int = 0
    skipped_low_share: int = 0
    per_template: dict[str, int] = field(default_factory=dict)


def mine_boilerplate(session: Session, *, tenant_id: uuid.UUID, min_share: float = MIN_CORPUS_SHARE, verified_only: bool = False) -> BoilerplateMiningResult:
    """Rank the normal statements this lab actually writes, per field."""
    result = BoilerplateMiningResult()

    stmt = select(Template.code, TemplateVersion.id, CorpusReport.report_text).join(TemplateVersion, TemplateVersion.template_id == Template.id).join(CorpusReportTemplateMap, CorpusReportTemplateMap.template_id == Template.id).join(CorpusReport, CorpusReport.id == CorpusReportTemplateMap.corpus_report_id).where(Template.tenant_id == tenant_id, TemplateVersion.is_current.is_(True))
    if verified_only:
        stmt = stmt.where(CorpusReportTemplateMap.is_verified.is_(True))

    # {version_id: {normalised_label: Counter(statement)}}
    tally: dict[uuid.UUID, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    report_totals: Counter[uuid.UUID] = Counter()
    template_of_version: dict[uuid.UUID, str] = {}

    for template_code, version_id, report_text in session.execute(stmt).all():
        template_of_version[version_id] = template_code
        report_totals[version_id] += 1
        for label, value in _FIELD_LINE.findall(report_text):
            if not _NORMAL_HINT.search(value):
                continue
            tally[version_id][_normalise_label(label)][value.strip()] += 1

    for version_id, per_label in tally.items():
        fields = list(session.execute(select(TemplateField).where(TemplateField.tenant_id == tenant_id, TemplateField.template_version_id == version_id)).scalars().all())
        total_reports = max(1, report_totals[version_id])
        written_here = 0

        for template_field in fields:
            result.fields_scanned += 1
            statements = per_label.get(_normalise_label(template_field.display_label))
            if not statements:
                continue

            for statement, count in statements.most_common(5):
                share = count / total_reports
                if share < min_share:
                    result.skipped_low_share += 1
                    continue
                if _upsert_candidate(session, tenant_id=tenant_id, template_field=template_field, text=statement, frequency=count, share=share):
                    result.candidates_written += 1
                    written_here += 1

        if written_here:
            code = template_of_version.get(version_id, str(version_id))
            result.per_template[code] = result.per_template.get(code, 0) + written_here

    session.flush()
    log.info("s5_boilerplate_mined", tenant_id=str(tenant_id), candidates_written=result.candidates_written, fields_scanned=result.fields_scanned, skipped_low_share=result.skipped_low_share)
    return result


def _normalise_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _upsert_candidate(session: Session, *, tenant_id: uuid.UUID, template_field: TemplateField, text: str, frequency: int, share: float) -> bool:
    """Refresh a candidate's counts, or write a new one. Returns True if new."""
    existing = session.execute(select(BoilerplateCandidate).where(BoilerplateCandidate.tenant_id == tenant_id, BoilerplateCandidate.template_field_id == template_field.id, BoilerplateCandidate.candidate_text == text)).scalar_one_or_none()
    if existing is not None:
        existing.corpus_frequency = frequency
        existing.corpus_share = round(share, 4)
        return False

    session.add(BoilerplateCandidate(tenant_id=tenant_id, template_field_id=template_field.id, candidate_text=text, corpus_frequency=frequency, corpus_share=round(share, 4), review_status=ReviewStatus.PENDING))
    return True


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate_id: uuid.UUID
    template_code: str
    section: str
    field_key: str
    display_label: str
    is_critical: bool
    absence_policy: str
    candidate_text: str
    corpus_frequency: int
    corpus_share: float
    review_status: str


def rank_candidates(session: Session, *, tenant_id: uuid.UUID, limit: int = TOP_N_FOR_REVIEW) -> list[RankedCandidate]:
    """The top ~40 by `corpus_share` — Pass 2's worksheet."""
    rows = session.execute(select(BoilerplateCandidate, TemplateField, Template.code).join(TemplateField, TemplateField.id == BoilerplateCandidate.template_field_id).join(TemplateVersion, TemplateVersion.id == TemplateField.template_version_id).join(Template, Template.id == TemplateVersion.template_id).where(BoilerplateCandidate.tenant_id == tenant_id, BoilerplateCandidate.review_status == ReviewStatus.PENDING).order_by(BoilerplateCandidate.corpus_share.desc()).limit(limit)).all()

    return [RankedCandidate(candidate_id=candidate.id, template_code=template_code, section=template_field.section, field_key=template_field.field_key, display_label=template_field.display_label, is_critical=template_field.is_critical, absence_policy=template_field.absence_policy, candidate_text=candidate.candidate_text, corpus_frequency=candidate.corpus_frequency or 0, corpus_share=float(candidate.corpus_share or 0.0), review_status=candidate.review_status) for candidate, template_field, template_code in rows]


def export_candidates_csv(session: Session, *, tenant_id: uuid.UUID, limit: int = TOP_N_FOR_REVIEW) -> str:
    """The V1 deliverable for boilerplate ranking: a CSV, not a screen."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["candidate_id", "template", "section", "field", "is_critical", "current_absence_policy", "candidate_text", "corpus_frequency", "corpus_share", "decision (approve/reject)"])
    for row in rank_candidates(session, tenant_id=tenant_id, limit=limit):
        writer.writerow([row.candidate_id, row.template_code, row.section, row.display_label, "CRITICAL — never auto-filled" if row.is_critical else "", row.absence_policy, row.candidate_text, row.corpus_frequency, f"{row.corpus_share:.4f}", ""])
    return buffer.getvalue()


def promote_candidate(session: Session, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, approved_by: uuid.UUID, enable_auto_fill: bool = False) -> TemplateField:
    """Attach a mined statement to a field as its `default_normal_text`."""
    candidate = session.get(BoilerplateCandidate, candidate_id)
    if candidate is None or candidate.tenant_id != tenant_id:
        raise ValueError(f"no boilerplate_candidate {candidate_id} in this tenant")

    template_field = session.get(TemplateField, candidate.template_field_id)
    if template_field is None or template_field.tenant_id != tenant_id:
        raise ValueError("boilerplate candidate points at a field in another tenant")

    if enable_auto_fill and template_field.is_critical:
        raise ValueError(f"field {template_field.field_key!r} is critical: it is never auto-filled under any circumstance. Store the text if you like, but absence_policy stays leave_blank_flag.")

    before = {"default_normal_text": template_field.default_normal_text, "absence_policy": template_field.absence_policy}
    template_field.default_normal_text = candidate.candidate_text
    if enable_auto_fill:
        template_field.absence_policy = AbsencePolicy.DEFAULT_NORMAL
    candidate.review_status = ReviewStatus.APPROVED

    session.add(AuditLog(tenant_id=tenant_id, actor_id=approved_by, actor_type=ActorType.USER, action="boilerplate_promoted", entity_type="template_field", entity_id=template_field.id, before=before, after={"default_normal_text": template_field.default_normal_text, "absence_policy": template_field.absence_policy, "auto_fill_enabled": enable_auto_fill}))
    session.flush()

    if enable_auto_fill:
        log.warning("s5_auto_fill_enabled", tenant_id=str(tenant_id), template_field_id=str(template_field.id), field_key=template_field.field_key, detail="this field will now assert normality nobody dictated ( Pass 2)")
    return template_field


def reject_candidate(session: Session, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, rejected_by: uuid.UUID) -> BoilerplateCandidate:
    """Take a statement out of the review queue for good."""
    candidate = session.get(BoilerplateCandidate, candidate_id)
    if candidate is None or candidate.tenant_id != tenant_id:
        raise ValueError(f"no boilerplate_candidate {candidate_id} in this tenant")

    candidate.review_status = ReviewStatus.REJECTED
    session.add(AuditLog(tenant_id=tenant_id, actor_id=rejected_by, actor_type=ActorType.USER, action="boilerplate_rejected", entity_type="boilerplate_candidate", entity_id=candidate.id, after={"candidate_text": candidate.candidate_text}))
    session.flush()
    return candidate
