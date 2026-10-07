"""The seven checks a lab must pass before it leaves onboarding for a live pilot.

Order: load_facts reads everything the checks look at in one statement, then the checks
judge those facts independently -- collision audit clear (check_collision_audit_clear),
corpus template coverage (check_corpus_template_coverage), voice enrollment
(check_voice_enrollment_complete), gold set frozen (check_gold_set_frozen), critical rules
approved (check_critical_rules_approved), baseline error rate measured
(check_baseline_cse_measured) and template library ready (check_template_library_ready).
evaluate_readiness runs all seven and returns the verdict.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import CheckStatus, CollisionSeverity
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.identity import RadiologistProfile
from radreport.db.models.knowledge import AutonomyClass, Template, TemplateVersion
from radreport.db.models.onboarding import CollisionAuditFinding, CorpusReportTemplateMap, OnboardingReadinessCheck
from radreport.db.models.reporting import CriticalFindingRule

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    check_id: str
    status: str
    measured_value: float | None = None
    threshold: float | None = None
    detail: dict[str, object] | None = None
    severity_on_failure: str = CheckStatus.FAIL
    """`fail` blocks the pilot transition; `warn` is recorded and does not."""


@dataclass(frozen=True, slots=True)
class ReadinessFacts:
    """Everything the seven checks look at, read in one statement."""

    outstanding_block_findings: int
    verified_mappings: int
    profiles: int
    missing_consent: list[str]
    missing_embedding: list[str]
    eval_sets: int
    frozen_item_counts: dict[str, int] | None
    """Items per frozen acceptance set; None when no set is frozen."""
    active_rules: int
    approved_rules: int
    autonomy_classes: int
    classes_without_baseline: list[str]
    approved_current_versions: int


CheckFn = Callable[[ReadinessFacts], CheckOutcome]


def _count(model: type, *where: Any) -> Any:
    return select(func.count()).select_from(model).where(*where).scalar_subquery()


def _ids(column: Any, *where: Any) -> Any:
    return select(func.coalesce(func.array_agg(cast(column, String)), literal([], ARRAY(String)))).where(*where).scalar_subquery()


def load_facts(session: Session, tenant_id: uuid.UUID) -> ReadinessFacts:
    """Read every fact the readiness checks need in a single statement of scalar subqueries."""
    profile = RadiologistProfile.tenant_id == tenant_id
    frozen = select(EvalSet.id.label("id"), func.count(EvalItem.id).label("n")).outerjoin(EvalItem, EvalItem.eval_set_id == EvalSet.id).where(EvalSet.tenant_id == tenant_id, EvalSet.is_frozen.is_(True)).group_by(EvalSet.id).subquery()
    rule = (CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.is_active.is_(True))
    row = session.execute(
        select(
            _count(CollisionAuditFinding, CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == "pending"),
            _count(CorpusReportTemplateMap, CorpusReportTemplateMap.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True)),
            _count(RadiologistProfile, profile),
            _ids(RadiologistProfile.id, profile, func.coalesce(RadiologistProfile.voice_consent_ref, "") == ""),
            _ids(RadiologistProfile.id, profile, RadiologistProfile.voice_embedding.is_(None)),
            _count(EvalSet, EvalSet.tenant_id == tenant_id),
            select(func.jsonb_object_agg(cast(frozen.c.id, String), frozen.c.n)).scalar_subquery(),
            _count(CriticalFindingRule, *rule),
            _count(CriticalFindingRule, *rule, CriticalFindingRule.approved_by.isnot(None)),
            _count(AutonomyClass, AutonomyClass.tenant_id == tenant_id),
            _ids(AutonomyClass.code, AutonomyClass.tenant_id == tenant_id, func.coalesce(AutonomyClass.baseline_cse_rate, 0) <= 0),
            select(func.count()).select_from(TemplateVersion).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True), TemplateVersion.approved_by.isnot(None), Template.is_active.is_(True)).scalar_subquery(),
        )
    ).one()
    return ReadinessFacts(*(list(v) if isinstance(v, list) else v for v in row))


# --------------------------------------------------------------- checks -----
def check_collision_audit_clear(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** Every `severity='block'` finding must be resolved."""
    outstanding = facts.outstanding_block_findings
    return CheckOutcome(check_id="collision_audit_clear", status=CheckStatus.PASS if outstanding == 0 else CheckStatus.FAIL, measured_value=float(outstanding), threshold=0.0, detail={"outstanding_block_findings": outstanding})


def check_corpus_template_coverage(facts: ReadinessFacts) -> CheckOutcome:
    """≥200 hand-verified report→template mappings."""
    verified = facts.verified_mappings
    threshold = 200
    return CheckOutcome(check_id="corpus_template_coverage", status=CheckStatus.PASS if verified >= threshold else CheckStatus.FAIL, measured_value=float(verified), threshold=float(threshold), detail={"verified_mappings": verified})


def check_voice_enrollment_complete(facts: ReadinessFacts) -> CheckOutcome:
    """Every dictating radiologist enrolled **with consent recorded**."""
    if not facts.profiles:
        return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.FAIL, detail={"reason": "no radiologist profiles in this tenant"})

    missing_consent, missing_embedding = facts.missing_consent, facts.missing_embedding
    ok = not missing_consent and not missing_embedding
    return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.PASS if ok else CheckStatus.FAIL, measured_value=float(facts.profiles - len(missing_consent) - len(missing_embedding)), threshold=float(facts.profiles), detail={"missing_consent": missing_consent[:10], "missing_embedding": missing_embedding[:10]})


def check_gold_set_frozen(facts: ReadinessFacts) -> CheckOutcome:
    """The per-lab **acceptance set** exists and is frozen."""
    if not facts.eval_sets:
        return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.FAIL, detail={"reason": "no per-lab acceptance eval_set exists"})
    if facts.frozen_item_counts is None:
        return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.FAIL, detail={"reason": "acceptance set exists but is not frozen"})

    threshold = 40
    counts = {k: int(v) for k, v in facts.frozen_item_counts.items()}
    largest = max(counts.values()) if counts else 0
    return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.PASS if largest >= threshold else CheckStatus.FAIL, measured_value=float(largest), threshold=float(threshold), detail={"item_counts": counts})


def check_critical_rules_approved(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** At least one radiologist-approved, active critical rule (critical-findings rules)."""
    approved = facts.approved_rules
    return CheckOutcome(check_id="critical_rules_approved", status=CheckStatus.PASS if approved else CheckStatus.FAIL, measured_value=float(approved), threshold=1.0, detail={"active_rules": facts.active_rules, "approved_rules": approved})


def check_baseline_cse_measured(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** The baseline audit has produced a real `baseline_cse_rate` for every autonomy class."""
    if not facts.autonomy_classes:
        return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.FAIL, detail={"reason": "no autonomy_class rows; baseline audit not recorded"})

    unmeasured = facts.classes_without_baseline
    return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.PASS if not unmeasured else CheckStatus.FAIL, measured_value=float(facts.autonomy_classes - len(unmeasured)), threshold=float(facts.autonomy_classes), detail={"classes_without_baseline": unmeasured})


def check_template_library_ready(facts: ReadinessFacts) -> CheckOutcome:
    """At least the pilot 20 templates are approved and current."""
    current = facts.approved_current_versions
    threshold = 20
    return CheckOutcome(
        check_id="template_library_ready",
        status=CheckStatus.PASS if current >= threshold else CheckStatus.WARN,
        measured_value=float(current),
        threshold=float(threshold),
        detail={"approved_current_versions": current},
        # Warn, not fail: a lab with a genuinely smaller catalogue is a real case picks 20 because it covers the power-law head, not because it is a safety floor.
        severity_on_failure=CheckStatus.WARN,
    )


ALL_CHECKS: tuple[CheckFn, ...] = (check_collision_audit_clear, check_corpus_template_coverage, check_voice_enrollment_complete, check_gold_set_frozen, check_critical_rules_approved, check_baseline_cse_measured, check_template_library_ready)


# ---------------------------------------------------------------- runner ----
@dataclass(slots=True)
class ReadinessReport:
    tenant_id: uuid.UUID
    outcomes: list[CheckOutcome]

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.status == CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.status == CheckStatus.WARN]

    @property
    def passed(self) -> bool:
        """True when no `fail`-severity check is outstanding."""
        return not self.failures


def evaluate_readiness(session: Session, tenant_id: uuid.UUID, *, persist: bool = True) -> ReadinessReport:
    """Run every readiness gate check and record the results."""
    facts = load_facts(session, tenant_id)
    outcomes: list[CheckOutcome] = []
    for check in ALL_CHECKS:
        outcome = check(facts)
        if outcome.status == CheckStatus.FAIL and outcome.severity_on_failure == CheckStatus.WARN:
            outcome = CheckOutcome(check_id=outcome.check_id, status=CheckStatus.WARN, measured_value=outcome.measured_value, threshold=outcome.threshold, detail=outcome.detail, severity_on_failure=CheckStatus.WARN)
        outcomes.append(outcome)

    if persist:
        session.add_all(OnboardingReadinessCheck(tenant_id=tenant_id, check_id=o.check_id, status=o.status, measured_value=o.measured_value, threshold=o.threshold, detail=o.detail) for o in outcomes)
        session.flush()

    report = ReadinessReport(tenant_id=tenant_id, outcomes=outcomes)
    log.info("s7_readiness_evaluated", tenant_id=str(tenant_id), passed=report.passed, failures=[o.check_id for o in report.failures], warnings=[o.check_id for o in report.warnings])
    return report
