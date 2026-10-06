"""The lab's historical signed reports, and the template map, usage counts and referrer ranking derived from them.

Order: read an uploaded file (parse_corpus_file) -> bulk-load the reports (load_corpus) -> work
out which template each one used
(derive_template_map) -> spot-check that mapping by hand (verify_mapping,
verification_progress) -> count how often each template is used (usage_histogram,
refresh_usage_counts) -> rank who refers the work (referrer_prior).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ActorType, ImportBatchType, ImportStatus, ImportTrigger, MatchMethod
from radreport.db.models.identity import RadiologistProfile
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap, ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.onboarding.batches import open_batch, record_counts, transition

log = get_logger(__name__)

#:  / readiness gate. Below this the histogram is not trusted.
VERIFIED_MAPPING_TARGET = 200

#: Structural-match floor.
MIN_STRUCTURAL_CONFIDENCE = 0.35

_FIELD_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /()'-]{1,60}?)\s*:", re.MULTILINE)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class CorpusRecord:
    """One historical signed report."""

    report_text: str
    external_report_id: str | None = None
    report_date: dt.date | dt.datetime | None = None
    radiologist_employee_code: str | None = None
    referring_doctor: str | None = None
    patient_sex: str | None = None
    patient_age_years: int | None = None
    is_deidentified: bool = False
    """**Gates every external API call**."""


#: Columns a corpus file may carry; only report_text is required.
CORPUS_COLUMNS = ("report_text", "external_report_id", "report_date", "radiologist_employee_code", "referring_doctor", "patient_sex", "patient_age_years", "is_deidentified")
_TRUE = frozenset({"true", "yes", "1", "y"})


def parse_corpus_file(data: bytes, filename: str) -> tuple[list[CorpusRecord], list[str]]:
    """Read a CSV (one report per row) or a JSON array of records. Returns `(records, problems)`."""
    text = data.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".json") or text.lstrip().startswith("["):
        try:
            raw_rows = json.loads(text)
        except ValueError as exc:
            return [], [f"not valid JSON: {exc}"]
        if not isinstance(raw_rows, list) or not all(isinstance(r, dict) for r in raw_rows):
            return [], ["a JSON corpus must be an array of objects"]
        rows = [{str(k).strip().lower(): v for k, v in r.items()} for r in raw_rows]
        first_line = 1
    else:
        reader = csv.DictReader(io.StringIO(text))
        if "report_text" not in {(name or "").strip().lower() for name in (reader.fieldnames or [])}:
            return [], ["missing required column: report_text"]
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
        first_line = 2

    records: list[CorpusRecord] = []
    problems: list[str] = []
    for line_no, row in enumerate(rows, start=first_line):
        unknown = sorted(set(row) - set(CORPUS_COLUMNS))
        if unknown:
            problems.append(f"row {line_no}: unknown column(s) {', '.join(unknown)}")
            continue
        report_text = str(row.get("report_text") or "").strip()
        if not report_text:
            problems.append(f"row {line_no}: report_text is empty")
            continue
        age_raw = row.get("patient_age_years")
        try:
            age = int(age_raw) if age_raw not in (None, "") else None
            report_date = dt.date.fromisoformat(str(row["report_date"])) if row.get("report_date") else None
        except (TypeError, ValueError):
            problems.append(f"row {line_no}: patient_age_years or report_date is malformed")
            continue
        deidentified = row.get("is_deidentified")
        records.append(CorpusRecord(report_text=report_text, external_report_id=str(row["external_report_id"]) if row.get("external_report_id") else None, report_date=report_date, radiologist_employee_code=str(row["radiologist_employee_code"]) if row.get("radiologist_employee_code") else None, referring_doctor=str(row["referring_doctor"]) if row.get("referring_doctor") else None, patient_sex=str(row["patient_sex"]) if row.get("patient_sex") else None, patient_age_years=age, is_deidentified=deidentified is True or str(deidentified).strip().lower() in _TRUE))
    return records, problems


@dataclass(slots=True)
class CorpusLoadResult:
    batch: ImportBatch
    loaded: int = 0
    duplicates: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)


def load_corpus(session: Session, *, tenant_id: uuid.UUID, records: list[CorpusRecord], submitted_by: uuid.UUID | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING, batch: ImportBatch | None = None) -> CorpusLoadResult:
    """Bulk-load signed reports. Idempotent by `external_report_id`."""
    batch = batch or open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.REPORT_CORPUS, stage="S2", trigger=trigger, submitted_by=submitted_by)
    if batch.status == ImportStatus.UPLOADING:
        transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)

    result = CorpusLoadResult(batch=batch)
    by_employee_code = _radiologist_index(session, tenant_id)

    existing_ids = {row for row in session.execute(select(CorpusReport.external_report_id).where(CorpusReport.tenant_id == tenant_id, CorpusReport.external_report_id.isnot(None))).scalars().all()}

    for record in records:
        if not record.report_text.strip():
            result.rejected.append((record.external_report_id or "<no id>", "empty report text"))
            continue
        if record.external_report_id and record.external_report_id in existing_ids:
            result.duplicates += 1
            continue

        session.add(CorpusReport(tenant_id=tenant_id, import_batch_id=batch.id, external_report_id=record.external_report_id, report_text=record.report_text, report_date=_as_datetime(record.report_date), radiologist_id=by_employee_code.get(record.radiologist_employee_code or ""), referring_doctor=record.referring_doctor, patient_sex=record.patient_sex, patient_age_years=record.patient_age_years, is_deidentified=record.is_deidentified))
        result.loaded += 1
        if record.external_report_id:
            existing_ids.add(record.external_report_id)

    batch.item_count += len(records)
    session.flush()
    record_counts(session, batch, accepted=result.loaded, rejected=len(result.rejected))

    log.info("s2_corpus_loaded", tenant_id=str(tenant_id), batch_id=str(batch.id), loaded=result.loaded, duplicates=result.duplicates, rejected=len(result.rejected))
    return result


def _as_datetime(value: dt.date | dt.datetime | None) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, dt.time.min)


def _radiologist_index(session: Session, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """`employee_code -> radiologist_profile.id`, for corpus attribution."""
    from radreport.db.models.identity import AppUser

    rows = session.execute(select(AppUser.employee_code, RadiologistProfile.id).join(RadiologistProfile, RadiologistProfile.user_id == AppUser.id).where(AppUser.tenant_id == tenant_id)).all()
    return {code: profile_id for code, profile_id in rows}


# ------------------------------------------------------------- mapping -----
@dataclass(slots=True)
class MappingResult:
    mapped: int = 0
    unmapped: int = 0
    by_method: Counter[str] = field(default_factory=Counter)


def derive_template_map(session: Session, *, tenant_id: uuid.UUID, batch: ImportBatch | None = None, min_confidence: float = MIN_STRUCTURAL_CONFIDENCE) -> MappingResult:
    """Map each corpus report to a template."""
    result = MappingResult()
    templates = _template_field_index(session, tenant_id)
    if not templates:
        log.warning("s2_no_templates_for_mapping", tenant_id=str(tenant_id))
        return result

    stmt = select(CorpusReport).where(CorpusReport.tenant_id == tenant_id)
    if batch is not None:
        stmt = stmt.where(CorpusReport.import_batch_id == batch.id)

    already_mapped = {row for row in session.execute(select(CorpusReportTemplateMap.corpus_report_id).where(CorpusReportTemplateMap.tenant_id == tenant_id)).scalars().all()}

    for report in session.execute(stmt).scalars():
        if report.id in already_mapped:
            continue

        match = _match_report(report.report_text, templates, min_confidence)
        if match is None:
            result.unmapped += 1
            continue

        template_id, method, confidence = match
        session.add(CorpusReportTemplateMap(tenant_id=tenant_id, corpus_report_id=report.id, template_id=template_id, match_method=method, confidence=round(confidence, 4), is_verified=False))
        result.mapped += 1
        result.by_method[method] += 1

    session.flush()
    log.info("s2_template_map_derived", tenant_id=str(tenant_id), mapped=result.mapped, unmapped=result.unmapped, by_method=dict(result.by_method))
    return result


@dataclass(frozen=True, slots=True)
class _TemplateIndex:
    template_id: uuid.UUID
    code: str
    spoken_study_code: str
    field_keys: frozenset[str]
    label_tokens: frozenset[str]


def _template_field_index(session: Session, tenant_id: uuid.UUID) -> list[_TemplateIndex]:
    rows = session.execute(select(Template.id, Template.code, TemplateVersion.spoken_study_code, TemplateVersion.json_schema).join(TemplateVersion, TemplateVersion.template_id == Template.id).where(Template.tenant_id == tenant_id, Template.is_active.is_(True), TemplateVersion.is_current.is_(True))).all()

    index: list[_TemplateIndex] = []
    for template_id, code, spoken, schema in rows:
        properties = (schema or {}).get("properties") or {}
        labels: set[str] = set()
        for key, prop in properties.items():
            labels.update(_WORD.findall(key.lower()))
            labels.update(_WORD.findall(str(prop.get("title", "")).lower()))
        index.append(_TemplateIndex(template_id=template_id, code=code, spoken_study_code=spoken or "", field_keys=frozenset(properties.keys()), label_tokens=frozenset(labels)))
    return index


def _match_report(text: str, templates: list[_TemplateIndex], min_confidence: float) -> tuple[uuid.UUID, str, float] | None:
    lowered = text.lower()

    for index in templates:
        spoken = index.spoken_study_code.lower().strip()
        if spoken and spoken in lowered:
            return index.template_id, MatchMethod.EXPLICIT, 0.95
        if index.code and index.code.lower() in lowered:
            return index.template_id, MatchMethod.EXPLICIT, 0.90

    report_labels = {token for label in _FIELD_LABEL.findall(text) for token in _WORD.findall(label.lower())}
    if not report_labels:
        return None

    best: tuple[uuid.UUID, float] | None = None
    for index in templates:
        if not index.label_tokens:
            continue
        overlap = len(report_labels & index.label_tokens)
        if not overlap:
            continue
        score = overlap / len(report_labels | index.label_tokens)
        if best is None or score > best[1]:
            best = (index.template_id, score)

    if best is None or best[1] < min_confidence:
        return None
    return best[0], MatchMethod.STRUCTURAL, best[1]


def verify_mapping(session: Session, *, tenant_id: uuid.UUID, mapping_id: uuid.UUID, verified_by: uuid.UUID, correct_template_id: uuid.UUID | None = None) -> CorpusReportTemplateMap:
    """The corpus load human gate: confirm or correct one derived mapping."""
    mapping = session.get(CorpusReportTemplateMap, mapping_id)
    if mapping is None or mapping.tenant_id != tenant_id:
        raise ValueError(f"no corpus_report_template_map {mapping_id} in this tenant")

    before = {"template_id": str(mapping.template_id), "match_method": mapping.match_method, "is_verified": mapping.is_verified}
    if correct_template_id is not None and correct_template_id != mapping.template_id:
        mapping.template_id = correct_template_id
        mapping.match_method = MatchMethod.HUMAN
        mapping.confidence = 1.0
    mapping.is_verified = True

    session.add(AuditLog(tenant_id=tenant_id, actor_id=verified_by, actor_type=ActorType.USER, action="corpus_mapping_verified", entity_type="corpus_report_template_map", entity_id=mapping.id, before=before, after={"template_id": str(mapping.template_id), "match_method": mapping.match_method, "is_verified": True}))
    session.flush()
    return mapping


def verification_progress(session: Session, tenant_id: uuid.UUID) -> tuple[int, int]:
    """`(verified, target)` — what the readiness gate stage's `corpus_template_coverage` check reads."""
    verified = session.execute(select(func.count()).select_from(CorpusReportTemplateMap).where(CorpusReportTemplateMap.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True))).scalar_one()
    return verified, VERIFIED_MAPPING_TARGET


# ----------------------------------------------------------- histograms -----
@dataclass(frozen=True, slots=True)
class HistogramEntry:
    template_id: uuid.UUID
    code: str
    count: int
    share: float
    cumulative_share: float


def usage_histogram(session: Session, *, tenant_id: uuid.UUID, verified_only: bool = False) -> list[HistogramEntry]:
    """Report volume per template, descending — the power-law head."""
    stmt = select(Template.id, Template.code, func.count(CorpusReportTemplateMap.id)).join(CorpusReportTemplateMap, CorpusReportTemplateMap.template_id == Template.id).where(CorpusReportTemplateMap.tenant_id == tenant_id).group_by(Template.id, Template.code).order_by(func.count(CorpusReportTemplateMap.id).desc())
    if verified_only:
        stmt = stmt.where(CorpusReportTemplateMap.is_verified.is_(True))

    rows = session.execute(stmt).all()
    total = sum(count for _, _, count in rows) or 1

    entries: list[HistogramEntry] = []
    cumulative = 0
    for template_id, code, count in rows:
        cumulative += count
        entries.append(HistogramEntry(template_id=template_id, code=code, count=count, share=round(count / total, 4), cumulative_share=round(cumulative / total, 4)))
    return entries


def refresh_usage_counts(session: Session, *, tenant_id: uuid.UUID) -> int:
    """Write the histogram back to `template.usage_count_12m`."""
    counts = {e.template_id: e.count for e in usage_histogram(session, tenant_id=tenant_id)}
    templates = list(session.execute(select(Template).where(Template.tenant_id == tenant_id)).scalars().all())
    for template in templates:
        template.usage_count_12m = counts.get(template.id, 0)
    session.flush()
    return len(templates)


def referrer_prior(session: Session, *, tenant_id: uuid.UUID, min_reports: int = 5) -> dict[str, dict[str, float]]:
    """P(template | referring doctor), for the routing prior."""
    rows = session.execute(select(CorpusReport.referring_doctor, Template.code).join(CorpusReportTemplateMap, CorpusReportTemplateMap.corpus_report_id == CorpusReport.id).join(Template, Template.id == CorpusReportTemplateMap.template_id).where(CorpusReport.tenant_id == tenant_id, CorpusReport.referring_doctor.isnot(None))).all()

    tally: dict[str, Counter[str]] = defaultdict(Counter)
    for referrer, template_code in rows:
        tally[referrer][template_code] += 1

    prior: dict[str, dict[str, float]] = {}
    for referrer, counts in tally.items():
        total = sum(counts.values())
        if total < min_reports:
            continue
        prior[referrer] = {code: round(n / total, 4) for code, n in counts.most_common()}
    return prior
