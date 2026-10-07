"""Defines the classes of reports autonomy is earned for, each with the measured baseline error rate it must not exceed.

Order: a product admin names a class, gives its measured baseline CSE rate (from grading already
signed reports), the non-inferiority margin and the number of graded reports the evidence needs,
and attaches the lab's templates to it (define_class; refused once the class has started accruing,
because changing the baseline then would move the goalposts) -> list a lab's classes (list_classes).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from radreport.autonomy.grant import DEFAULT_CUSUM_THRESHOLD
from radreport.core.types import ActorType, AutonomyStatus
from radreport.db.models.knowledge import AutonomyClass, Template
from radreport.db.models.orchestration import AuditLog


class ClassRefused(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClassSpec:
    code: str
    display_name: str
    baseline_cse_rate: float
    required_n: int
    template_codes: tuple[str, ...]
    ni_margin_pp: float = 1.0


def define_class(session: Session, tenant_id: uuid.UUID, spec: ClassSpec, *, platform_user_id: uuid.UUID) -> AutonomyClass:
    """Create the class, or change it while it is still not evaluated, and point its templates at it."""
    if not 0 < spec.baseline_cse_rate < 1:
        raise ClassRefused("baseline_cse_rate must be a measured rate between 0 and 1; grade signed reports to measure it")
    templates = list(session.execute(select(Template).where(Template.tenant_id == tenant_id, Template.code.in_(spec.template_codes))).scalars())
    missing = sorted(set(spec.template_codes) - {t.code for t in templates})
    if missing:
        raise ClassRefused(f"no template {', '.join(missing)} in this lab")
    klass = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id, AutonomyClass.code == spec.code)).scalar_one_or_none()
    before = None
    if klass is None:
        klass = AutonomyClass(tenant_id=tenant_id, code=spec.code, display_name=spec.display_name, status=AutonomyStatus.NOT_EVALUATED, baseline_cse_rate=spec.baseline_cse_rate, ni_margin_pp=spec.ni_margin_pp, required_n=spec.required_n, cusum_threshold=DEFAULT_CUSUM_THRESHOLD)
        session.add(klass)
        session.flush()
    elif klass.status != AutonomyStatus.NOT_EVALUATED:
        raise ClassRefused(f"{spec.code} is {klass.status}; its baseline and size are fixed once evidence is being collected")
    else:
        before = {"baseline_cse_rate": float(klass.baseline_cse_rate), "required_n": klass.required_n}
        klass.display_name, klass.baseline_cse_rate, klass.ni_margin_pp, klass.required_n = spec.display_name, spec.baseline_cse_rate, spec.ni_margin_pp, spec.required_n
    session.execute(update(Template).where(Template.tenant_id == tenant_id, Template.id.in_([t.id for t in templates])).values(autonomy_class_id=klass.id))
    session.add(AuditLog(tenant_id=tenant_id, actor_id=platform_user_id, actor_type=ActorType.USER, action="autonomy_class_defined", entity_type="autonomy_class", entity_id=klass.id, before=before, after={"code": spec.code, "baseline_cse_rate": spec.baseline_cse_rate, "required_n": spec.required_n, "templates": sorted(spec.template_codes)}))
    session.flush()
    return klass


def list_classes(session: Session, tenant_id: uuid.UUID) -> list[dict[str, object]]:
    rows = session.execute(select(AutonomyClass).where(AutonomyClass.tenant_id == tenant_id).order_by(AutonomyClass.code)).scalars().all()
    members: dict[uuid.UUID, list[str]] = {}
    for code, class_id in session.execute(select(Template.code, Template.autonomy_class_id).where(Template.tenant_id == tenant_id, Template.autonomy_class_id.isnot(None))).all():
        members.setdefault(class_id, []).append(code)
    return [{"code": k.code, "display_name": k.display_name, "status": k.status, "baseline_cse_rate": float(k.baseline_cse_rate), "ni_margin_pp": float(k.ni_margin_pp), "required_n": k.required_n, "accrued_n": k.accrued_n, "templates": sorted(members.get(k.id, []))} for k in rows]
