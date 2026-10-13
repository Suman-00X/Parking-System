"""Measures an autonomy class's baseline error rate: an independent grader re-reads a random sample of the lab's own signed reports.

Order: draw the reports to grade from those mapped to the class's templates (draw_sample,
export_sample_csv) -> read the grades the grader sends back (parse_grades_csv) and record them
(record_grades) -> once enough are graded, set the class's baseline from them
(compute_baseline; audit_progress and progress_by_class say how far along it is). A class's
baseline is fixed once it starts collecting evidence, so grading stops there too.
"""

from __future__ import annotations

import csv
import io
import random
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.core.logging import get_logger
from radreport.core.types import CSE_GRADES, ActorType, AutonomyStatus, SeverityGrade
from radreport.db.models.knowledge import AutonomyClass, Template
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import BaselineAuditGrade

log = get_logger(__name__)

#: Columns a returned grades file carries.
GRADE_COLUMNS = ("external_report_id", "severity_grade", "grader")


class BaselineRefused(ValueError):
    """A baseline-audit step that cannot run as asked."""


@dataclass(frozen=True, slots=True)
class GradeRow:
    external_report_id: str
    severity_grade: str
    grader: str


@dataclass(slots=True)
class GradeResult:
    recorded: int = 0
    regraded: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)


def min_graded(session: Session, tenant_id: uuid.UUID) -> int:
    """How many graded reports this lab's baselines need."""
    return int(system_config.resolve(session, "autonomy.baseline_min_graded", tenant_id=tenant_id).value)


def _class(session: Session, tenant_id: uuid.UUID, class_code: str) -> AutonomyClass:
    klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == class_code)).scalar_one_or_none()
    if klass is None:
        raise BaselineRefused(f"no autonomy class {class_code!r} in this lab")
    return klass


def _gradable(tenant_id: uuid.UUID, klass: AutonomyClass) -> Select[tuple[CorpusReport]]:
    """Reports a human has confirmed belong to one of the class's templates, and that carry an id a grader can send back."""
    return select(CorpusReport).join(CorpusReportTemplateMap, CorpusReportTemplateMap.corpus_report_id == CorpusReport.id).join(Template, Template.id == CorpusReportTemplateMap.template_id).where(CorpusReport.tenant_id == tenant_id, Template.autonomy_class_id == klass.id, CorpusReportTemplateMap.is_verified.is_(True), CorpusReport.external_report_id.isnot(None))


def audit_progress(session: Session, tenant_id: uuid.UUID, klass: AutonomyClass) -> tuple[int, int]:
    """`(graded, clinically significant errors)` so far for one class."""
    graded, cse = session.execute(select(func.count(), func.count().filter(BaselineAuditGrade.is_cse.is_(True))).where(BaselineAuditGrade.tenant_id == tenant_id, BaselineAuditGrade.autonomy_class_id == klass.id)).one()
    return int(graded), int(cse)


def progress_by_class(session: Session, tenant_id: uuid.UUID) -> list[tuple[str, str, float, int, int]]:
    """`(code, status, baseline, graded, significant errors)` for every class in the lab, in one read."""
    rows = session.execute(select(AutonomyClass.code, AutonomyClass.status, AutonomyClass.baseline_cse_rate, func.count(BaselineAuditGrade.id), func.count(BaselineAuditGrade.id).filter(BaselineAuditGrade.is_cse.is_(True))).outerjoin(BaselineAuditGrade, (BaselineAuditGrade.autonomy_class_id == AutonomyClass.id) & (BaselineAuditGrade.tenant_id == AutonomyClass.tenant_id)).where(AutonomyClass.tenant_id == tenant_id).group_by(AutonomyClass.id).order_by(AutonomyClass.code)).all()
    return [(code, status, float(rate or 0), int(graded), int(cse)) for code, status, rate, graded, cse in rows]


def draw_sample(session: Session, *, tenant_id: uuid.UUID, class_code: str, size: int | None = None, seed: int = 0) -> list[CorpusReport]:
    """A random, not yet graded slice of the class's reports; by default exactly what the audit still needs."""
    klass = _class(session, tenant_id, class_code)
    graded_ids = select(BaselineAuditGrade.corpus_report_id).where(BaselineAuditGrade.tenant_id == tenant_id, BaselineAuditGrade.autonomy_class_id == klass.id)
    pool = list(session.execute(_gradable(tenant_id, klass).where(CorpusReport.id.not_in(graded_ids)).order_by(CorpusReport.id)).scalars().all())
    wanted = size if size is not None else max(0, min_graded(session, tenant_id) - audit_progress(session, tenant_id, klass)[0])
    # Seeded, so the sample a grader was sent can be drawn again for an audit of the audit.
    random.Random(seed).shuffle(pool)
    return pool[:wanted]


def export_sample_csv(reports: list[CorpusReport]) -> str:
    """The sample as a CSV the grader fills in: one row per report, grade and grader left blank."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("external_report_id", "report_date", "report_text", "severity_grade", "grader"))
    for report in reports:
        writer.writerow((report.external_report_id, report.report_date.date().isoformat() if report.report_date else "", report.report_text, "", ""))
    return out.getvalue()


def parse_grades_csv(data: bytes) -> tuple[list[GradeRow], list[str]]:
    """The grader's returned file. Returns `(rows, problems)`; a row with no grade yet is skipped, not an error."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    names = {(n or "").strip().lower() for n in reader.fieldnames or []}
    missing = [c for c in GRADE_COLUMNS if c not in names]
    if missing:
        return [], [f"missing required column(s): {', '.join(missing)}"]
    rows: list[GradeRow] = []
    problems: list[str] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        grade = row["severity_grade"].upper()
        if not grade:
            continue
        if grade not in SeverityGrade.values():
            problems.append(f"row {line_no}: severity_grade {row['severity_grade']!r} is not one of {', '.join(SeverityGrade.values())}")
            continue
        if not row["external_report_id"] or not row["grader"]:
            problems.append(f"row {line_no}: external_report_id and grader are both required")
            continue
        rows.append(GradeRow(external_report_id=row["external_report_id"], severity_grade=grade, grader=row["grader"]))
    return rows, problems


def record_grades(session: Session, *, tenant_id: uuid.UUID, class_code: str, rows: list[GradeRow], actor_id: uuid.UUID | None = None) -> GradeResult:
    """Store each grade against its report; a report graded again keeps only its latest grade."""
    klass = _class(session, tenant_id, class_code)
    if klass.status != AutonomyStatus.NOT_EVALUATED:
        raise BaselineRefused(f"{class_code} is {klass.status}; its baseline was fixed when evidence collection started")
    gradable = {r.external_report_id: r.id for r in session.execute(_gradable(tenant_id, klass).where(CorpusReport.external_report_id.in_([r.external_report_id for r in rows]))).scalars()}
    existing = {g.corpus_report_id: g for g in session.execute(select(BaselineAuditGrade).where(BaselineAuditGrade.tenant_id == tenant_id, BaselineAuditGrade.autonomy_class_id == klass.id, BaselineAuditGrade.corpus_report_id.in_(list(gradable.values())))).scalars()}
    result = GradeResult()
    for row in rows:
        report_id = gradable.get(row.external_report_id)
        if report_id is None:
            result.rejected.append((row.external_report_id, f"not a verified report of a template in {class_code}"))
            continue
        is_cse = row.severity_grade in CSE_GRADES
        prior = existing.get(report_id)
        if prior is not None:
            prior.severity_grade, prior.is_cse, prior.grader = row.severity_grade, is_cse, row.grader
            result.regraded += 1
            continue
        grade = BaselineAuditGrade(tenant_id=tenant_id, autonomy_class_id=klass.id, corpus_report_id=report_id, severity_grade=row.severity_grade, is_cse=is_cse, grader=row.grader)
        session.add(grade)
        existing[report_id] = grade
        result.recorded += 1
    session.flush()
    graded, cse = audit_progress(session, tenant_id, klass)
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="baseline_grades_recorded", entity_type="autonomy_class", entity_id=klass.id, after={"recorded": result.recorded, "regraded": result.regraded, "rejected": len(result.rejected), "graded_total": graded, "cse_total": cse}))
    session.flush()
    return result


def compute_baseline(session: Session, *, tenant_id: uuid.UUID, class_code: str, actor_id: uuid.UUID | None = None) -> AutonomyClass:
    """Set the class's baseline from its grades, once there are enough of them."""
    klass = _class(session, tenant_id, class_code)
    if klass.status != AutonomyStatus.NOT_EVALUATED:
        raise BaselineRefused(f"{class_code} is {klass.status}; its baseline was fixed when evidence collection started")
    graded, cse = audit_progress(session, tenant_id, klass)
    floor = min_graded(session, tenant_id)
    if graded < floor:
        raise BaselineRefused(f"{class_code} has {graded} graded report(s); its baseline needs at least {floor}")
    # The Beta(0.5, 0.5) posterior mean: close to cse/graded, and never zero, so the monitor always has a rate to compare with.
    rate = round((cse + 0.5) / (graded + 1), 5)
    before = float(klass.baseline_cse_rate or 0)
    klass.baseline_cse_rate = rate
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="baseline_measured", entity_type="autonomy_class", entity_id=klass.id, before={"baseline_cse_rate": before}, after={"baseline_cse_rate": rate, "graded": graded, "cse": cse}))
    session.flush()
    log.info("baseline_measured", tenant_id=str(tenant_id), class_code=class_code, graded=graded, cse=cse, rate=rate)
    return klass
