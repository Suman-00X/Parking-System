"""The seven checks a lab must pass before it leaves onboarding for a live pilot.

Order: the checks are independent -- collision audit clear (check_collision_audit_clear),
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

from sqlalchemy import func, select
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


CheckFn = Callable[[Session, uuid.UUID], CheckOutcome]


# --------------------------------------------------------------- checks -----
def check_collision_audit_clear(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """**Blocking.** Every `severity='block'` finding must be resolved."""
    outstanding = session.execute(select(func.count()).select_from(CollisionAuditFinding).where(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == "pending")).scalar_one()

    return CheckOutcome(check_id="collision_audit_clear", status=CheckStatus.PASS if outstanding == 0 else CheckStatus.FAIL, measured_value=float(outstanding), threshold=0.0, detail={"outstanding_block_findings": outstanding})


def check_corpus_template_coverage(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """≥200 hand-verified report→template mappings."""
    verified = session.execute(select(func.count()).select_from(CorpusReportTemplateMap).where(CorpusReportTemplateMap.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True))).scalar_one()

    threshold = 200
    return CheckOutcome(check_id="corpus_template_coverage", status=CheckStatus.PASS if verified >= threshold else CheckStatus.FAIL, measured_value=float(verified), threshold=float(threshold), detail={"verified_mappings": verified})


def check_voice_enrollment_complete(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """Every dictating radiologist enrolled **with consent recorded**."""
    profiles = list(session.execute(select(RadiologistProfile).where(RadiologistProfile.tenant_id == tenant_id)).scalars().all())
    if not profiles:
        return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.FAIL, detail={"reason": "no radiologist profiles in this tenant"})

    missing_consent = [str(p.id) for p in profiles if not p.voice_consent_ref]
    missing_embedding = [str(p.id) for p in profiles if p.voice_embedding is None]

    ok = not missing_consent and not missing_embedding
    return CheckOutcome(check_id="voice_enrollment_complete", status=CheckStatus.PASS if ok else CheckStatus.FAIL, measured_value=float(len(profiles) - len(missing_consent) - len(missing_embedding)), threshold=float(len(profiles)), detail={"missing_consent": missing_consent[:10], "missing_embedding": missing_embedding[:10]})


def check_gold_set_frozen(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """The per-lab **acceptance set** exists and is frozen."""
    acceptance_sets = list(session.execute(select(EvalSet).where(EvalSet.tenant_id == tenant_id)).scalars().all())
    if not acceptance_sets:
        return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.FAIL, detail={"reason": "no per-lab acceptance eval_set exists"})

    threshold = 40
    frozen = [s for s in acceptance_sets if s.is_frozen]
    if not frozen:
        return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.FAIL, detail={"reason": "acceptance set exists but is not frozen"})

    # One grouped count for every frozen set, rather than a count per set.
    found = dict(session.execute(select(EvalItem.eval_set_id, func.count()).where(EvalItem.eval_set_id.in_([s.id for s in frozen])).group_by(EvalItem.eval_set_id)).all())
    counts = {str(s.id): int(found.get(s.id, 0)) for s in frozen}
    largest = max(counts.values()) if counts else 0

    return CheckOutcome(check_id="gold_set_frozen", status=CheckStatus.PASS if largest >= threshold else CheckStatus.FAIL, measured_value=float(largest), threshold=float(threshold), detail={"item_counts": counts})


def check_critical_rules_approved(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """**Blocking.** At least one radiologist-approved, active critical rule (critical-findings rules)."""
    rules = list(session.execute(select(CriticalFindingRule).where(CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.is_active.is_(True))).scalars().all())
    approved = [r for r in rules if r.approved_by is not None]

    return CheckOutcome(check_id="critical_rules_approved", status=CheckStatus.PASS if approved else CheckStatus.FAIL, measured_value=float(len(approved)), threshold=1.0, detail={"active_rules": len(rules), "approved_rules": len(approved)})


def check_baseline_cse_measured(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """**Blocking.**'s baseline audit has produced a real `baseline_cse_rate`."""
    classes = list(session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id)).scalars().all())
    if not classes:
        return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.FAIL, detail={"reason": "no autonomy_class rows; baseline audit not recorded"})

    unmeasured = [c.code for c in classes if not c.baseline_cse_rate or float(c.baseline_cse_rate) <= 0]
    return CheckOutcome(check_id="baseline_cse_measured", status=CheckStatus.PASS if not unmeasured else CheckStatus.FAIL, measured_value=float(len(classes) - len(unmeasured)), threshold=float(len(classes)), detail={"classes_without_baseline": unmeasured})


def check_template_library_ready(session: Session, tenant_id: uuid.UUID) -> CheckOutcome:
    """At least the pilot 20 templates are approved and current."""
    current = session.execute(select(func.count()).select_from(TemplateVersion).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True), TemplateVersion.approved_by.isnot(None), Template.is_active.is_(True))).scalar_one()

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
    outcomes: list[CheckOutcome] = []
    for check in ALL_CHECKS:
        outcome = check(session, tenant_id)
        if outcome.status == CheckStatus.FAIL and outcome.severity_on_failure == CheckStatus.WARN:
            outcome = CheckOutcome(check_id=outcome.check_id, status=CheckStatus.WARN, measured_value=outcome.measured_value, threshold=outcome.threshold, detail=outcome.detail, severity_on_failure=CheckStatus.WARN)
        outcomes.append(outcome)

    if persist:
        session.add_all(OnboardingReadinessCheck(tenant_id=tenant_id, check_id=o.check_id, status=o.status, measured_value=o.measured_value, threshold=o.threshold, detail=o.detail) for o in outcomes)
        session.flush()

    report = ReadinessReport(tenant_id=tenant_id, outcomes=outcomes)
    log.info("s7_readiness_evaluated", tenant_id=str(tenant_id), passed=report.passed, failures=[o.check_id for o in report.failures], warnings=[o.check_id for o in report.warnings])
    return report
