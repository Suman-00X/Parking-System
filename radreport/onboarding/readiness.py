"""The seven checks a lab must pass before it leaves onboarding for a live pilot.

Order: load_facts reads everything the checks look at in one statement (plus the gate run it
names), then the checks judge those facts independently -- collision audit clear
(check_collision_audit_clear), voice enrollment (check_voice_enrollment_complete), gold bundles
for every template in the frozen acceptance set (check_gold_bundles_per_template), critical
rules approved when alerts are on (check_critical_rules_approved), baseline error rate measured
by audit (check_baseline_cse_measured), accuracy on the acceptance set
(check_acceptance_accuracy) and template library ready (check_template_library_ready).
evaluate_readiness runs all seven and returns the verdict.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.core.logging import get_logger
from radreport.core.types import CheckStatus, CollisionSeverity
from radreport.db.models.evaluation import EvalItem, EvalRun, EvalSet
from radreport.db.models.identity import RadiologistProfile
from radreport.db.models.knowledge import AutonomyClass, Template, TemplateVersion
from radreport.db.models.onboarding import CollisionAuditFinding, OnboardingReadinessCheck
from radreport.db.models.reporting import BaselineAuditGrade, CriticalFindingRule
from radreport.eval.gates import evaluate_gate

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
    profiles: int
    missing_consent: list[str]
    missing_embedding: list[str]
    active_templates: int
    templates_short_of_gold: list[str]
    """Codes of active templates with fewer than `gold_per_template` items in the latest frozen acceptance set."""

    acceptance_set_id: uuid.UUID | None
    """The lab's most recently frozen acceptance set; None when none is frozen."""

    active_rules: int
    approved_rules: int
    autonomy_classes: int
    classes_without_baseline: list[str]
    """Classes whose baseline is unset or rests on fewer graded reports than the audit needs."""

    approved_current_versions: int
    gold_per_template: int = 4
    baseline_min_graded: int = 200
    critical_alerts_on: bool = False
    accuracy_run: EvalRun | None = None
    """The latest full release-gate run on the acceptance set."""


CheckFn = Callable[[ReadinessFacts], CheckOutcome]


def _count(model: type, *where: Any) -> Any:
    return select(func.count()).select_from(model).where(*where).scalar_subquery()


def _ids(column: Any, *where: Any) -> Any:
    return select(func.coalesce(func.array_agg(cast(column, String)), literal([], ARRAY(String)))).where(*where).scalar_subquery()


def _setting(session: Session, tenant_id: uuid.UUID, name: str) -> int:
    return int(system_config.resolve(session, name, tenant_id=tenant_id).value)


def load_facts(session: Session, tenant_id: uuid.UUID) -> ReadinessFacts:
    """Read every fact the readiness checks need in a single statement of scalar subqueries."""
    per_template = _setting(session, tenant_id, "onboarding.gold_per_template")
    min_graded = _setting(session, tenant_id, "autonomy.baseline_min_graded")
    alerts_on = bool(_setting(session, tenant_id, "critical_findings.enabled"))

    profile = RadiologistProfile.tenant_id == tenant_id
    rule = (CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.is_active.is_(True))
    live = exists().where(TemplateVersion.template_id == Template.id, TemplateVersion.is_current.is_(True), TemplateVersion.approved_by.isnot(None))
    active = (Template.tenant_id == tenant_id, Template.is_active.is_(True), live)
    latest_set = select(EvalSet.id).where(EvalSet.tenant_id == tenant_id, EvalSet.is_frozen.is_(True), EvalSet.is_canonical.is_(False)).order_by(EvalSet.created_at.desc(), EvalSet.id.desc()).limit(1).scalar_subquery()
    # Counted through the version's template, so a template re-versioned since the freeze keeps its items.
    held = select(func.count(EvalItem.id)).join(TemplateVersion, TemplateVersion.id == EvalItem.gold_template_version_id).where(EvalItem.eval_set_id == latest_set, TemplateVersion.template_id == Template.id).scalar_subquery()
    graded = select(func.count(BaselineAuditGrade.id)).where(BaselineAuditGrade.tenant_id == tenant_id, BaselineAuditGrade.autonomy_class_id == AutonomyClass.id).scalar_subquery()
    latest_run = select(EvalRun.id).where(EvalRun.eval_set_id == latest_set, EvalRun.is_release_gate.is_(True), EvalRun.is_smoke_subset.is_(False), EvalRun.completed_at.isnot(None)).order_by(EvalRun.completed_at.desc(), EvalRun.id.desc()).limit(1).scalar_subquery()
    row = session.execute(
        select(
            _count(CollisionAuditFinding, CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == "pending"),
            _count(RadiologistProfile, profile),
            _ids(RadiologistProfile.id, profile, func.coalesce(RadiologistProfile.voice_consent_ref, "") == ""),
            _ids(RadiologistProfile.id, profile, RadiologistProfile.voice_embedding.is_(None)),
            _count(Template, *active),
            _ids(Template.code, *active, held < per_template),
            latest_set,
            _count(CriticalFindingRule, *rule),
            _count(CriticalFindingRule, *rule, CriticalFindingRule.approved_by.isnot(None)),
            _count(AutonomyClass, AutonomyClass.tenant_id == tenant_id),
            _ids(AutonomyClass.code, AutonomyClass.tenant_id == tenant_id, or_(func.coalesce(AutonomyClass.baseline_cse_rate, 0) <= 0, graded < min_graded)),
            select(func.count()).select_from(TemplateVersion).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True), TemplateVersion.approved_by.isnot(None), Template.is_active.is_(True)).scalar_subquery(),
            latest_run,
        )
    ).one()
    *values, run_id = (list(v) if isinstance(v, list) else v for v in row)
    return ReadinessFacts(*values, gold_per_template=per_template, baseline_min_graded=min_graded, critical_alerts_on=alerts_on, accuracy_run=session.get(EvalRun, run_id) if run_id else None)


# --------------------------------------------------------------- checks -----
def check_collision_audit_clear(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** Every `severity='block'` finding must be resolved."""
    outstanding = facts.outstanding_block_findings
    return CheckOutcome(check_id="collision_audit_clear", status=CheckStatus.PASS if outstanding == 0 else CheckStatus.FAIL, measured_value=float(outstanding), threshold=0.0, detail={"outstanding_block_findings": outstanding})


def check_voice_enrollment_complete(facts: ReadinessFacts) -> CheckOutcome:
    """Every dictating radiologist enrolled **with consent recorded**."""
    if not facts.profiles:
        return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.FAIL, detail={"reason": "no radiologist profiles in this tenant"})

    missing_consent, missing_embedding = facts.missing_consent, facts.missing_embedding
    ok = not missing_consent and not missing_embedding
    return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.PASS if ok else CheckStatus.FAIL, measured_value=float(facts.profiles - len(missing_consent) - len(missing_embedding)), threshold=float(facts.profiles), detail={"missing_consent": missing_consent[:10], "missing_embedding": missing_embedding[:10]})


def check_gold_bundles_per_template(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** Every active template has its gold bundles in a frozen acceptance set, so every template is tested."""
    if not facts.active_templates:
        return CheckOutcome(check_id="gold_bundles_per_template", status=CheckStatus.FAIL, detail={"reason": "no approved, active templates yet", **({"acceptance_set_id": str(facts.acceptance_set_id)} if facts.acceptance_set_id else {})})
    if facts.acceptance_set_id is None:
        return CheckOutcome(check_id="gold_bundles_per_template", status=CheckStatus.FAIL, threshold=float(facts.gold_per_template), detail={"reason": "no acceptance set is frozen yet", "templates_short": facts.templates_short_of_gold[:20]})
    short = facts.templates_short_of_gold
    return CheckOutcome(check_id="gold_bundles_per_template", status=CheckStatus.PASS if not short else CheckStatus.FAIL, measured_value=float(facts.active_templates - len(short)), threshold=float(facts.active_templates), detail={"per_template": facts.gold_per_template, "templates_short": short[:20], "acceptance_set_id": str(facts.acceptance_set_id)})


def check_critical_rules_approved(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking when alerts are on.** At least one radiologist-approved, active critical rule; a warning when the lab has alerts switched off."""
    approved = facts.approved_rules
    detail: dict[str, object] = {"active_rules": facts.active_rules, "approved_rules": approved, "alerts_on": facts.critical_alerts_on}
    if not facts.critical_alerts_on:
        return CheckOutcome(check_id="critical_rules_approved", status=CheckStatus.WARN, measured_value=float(approved), threshold=1.0, detail={**detail, "reason": "critical-finding alerts are switched off for this lab"}, severity_on_failure=CheckStatus.WARN)
    return CheckOutcome(check_id="critical_rules_approved", status=CheckStatus.PASS if approved else CheckStatus.FAIL, measured_value=float(approved), threshold=1.0, detail=detail)


def check_baseline_cse_measured(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** Every autonomy class has a baseline error rate measured by grading enough of the lab's signed reports."""
    if not facts.autonomy_classes:
        return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.FAIL, detail={"reason": "no autonomy_class rows; baseline audit not recorded"})

    unmeasured = facts.classes_without_baseline
    return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.PASS if not unmeasured else CheckStatus.FAIL, measured_value=float(facts.autonomy_classes - len(unmeasured)), threshold=float(facts.autonomy_classes), detail={"classes_without_baseline": unmeasured, "min_graded": facts.baseline_min_graded})


def check_acceptance_accuracy(facts: ReadinessFacts) -> CheckOutcome:
    """**Blocking.** The latest full gate run on the frozen acceptance set meets the first-release limits."""
    run = facts.accuracy_run
    if run is None:
        return CheckOutcome(check_id="acceptance_accuracy", status=CheckStatus.FAIL, detail={"reason": "no completed release-gate run on the frozen acceptance set"})
    verdict = evaluate_gate(run, None)
    return CheckOutcome(check_id="acceptance_accuracy", status=CheckStatus.PASS if verdict.passed else CheckStatus.FAIL, detail={"eval_run_id": str(run.id), "summary": verdict.summary(), "regressions": verdict.regressions[:10], "metrics": {k: v for k, v in (run.metrics or {}).items() if isinstance(v, int | float)}})


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
        # Warn, not fail: a lab with a genuinely smaller catalogue is a real case; 20 covers the common studies, it is not a safety floor.
        severity_on_failure=CheckStatus.WARN,
    )


ALL_CHECKS: tuple[CheckFn, ...] = (check_collision_audit_clear, check_voice_enrollment_complete, check_gold_bundles_per_template, check_critical_rules_approved, check_baseline_cse_measured, check_acceptance_accuracy, check_template_library_ready)


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
