"""Registering a new lab and moving it through its lifecycle.

Order: register the lab (register_lab) -> move it between statuses as onboarding progresses
(transition_status) -> shut it down (offboard).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.tenancy import assert_transition_allowed
from radreport.core.types import ActorType, ImportBatchType, ImportStatus, ImportTrigger, TenantStatus, TrainingConsentEvent, UserRole
from radreport.db.models.evaluation import EvalSet
from radreport.db.models.identity import AppUser
from radreport.db.models.onboarding import ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.tenancy import Tenant, TenantBranding
from radreport.db.session import bind_tenant
from radreport.knowledge.consent import record_consent_event
from radreport.onboarding.readiness import evaluate_readiness

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LabRegistration:
    name: str
    slug: str
    admin_email: str
    admin_display_name: str
    admin_employee_code: str

    training_pooling_consent: bool = False
    """**Captured from the signed contract at registration.**: free to include before contract #1, near-impossible to retrofit across signed labs."""

    training_consent_ref: str | None = None
    patient_notice_version: str | None = None
    """Which patient-notice wording this lab adopted."""


@dataclass(slots=True)
class RegistrationResult:
    tenant: Tenant
    lab_admin: AppUser
    acceptance_eval_set: EvalSet
    onboarding_batch: ImportBatch


def register_lab(session: Session, registration: LabRegistration, *, actor_id: uuid.UUID | None = None) -> RegistrationResult:
    """Provision a new lab, landing it in `onboarding`."""
    existing = session.execute(select(Tenant).where(Tenant.slug == registration.slug)).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"tenant slug {registration.slug!r} is already taken")

    tenant = Tenant(
        name=registration.name,
        slug=registration.slug,
        status=TenantStatus.PROVISIONING,
        training_pooling_consent=False,  # set via the consent event below
        patient_notice_version=registration.patient_notice_version,
    )
    session.add(tenant)
    session.flush()

    # Everything below this line writes tenant-owned rows, so the session has to narrow onto the tenant it just created.
    bind_tenant(session, tenant.id)

    lab_admin = AppUser(
        tenant_id=tenant.id,
        employee_code=registration.admin_employee_code,
        display_name=registration.admin_display_name,
        email=registration.admin_email,
        # `lab_admin`, not `admin` — "admin" is reserved for the platform realm, and a lab admin must never be escalatable to product admin by editing a roles array.
        roles=[UserRole.LAB_ADMIN],
    )
    session.add(lab_admin)

    session.add(TenantBranding(tenant_id=tenant.id))

    acceptance_set = EvalSet(tenant_id=tenant.id, name=f"{registration.slug}-acceptance", description=("Per-lab acceptance set: ~40 items, readiness readiness only. Not a release gate — that is the canonical set."), is_frozen=False, is_canonical=False, stratification_spec={"target_size": 40, "stratify_by": ["capture_device_class", "template"]})
    session.add(acceptance_set)

    batch = ImportBatch(tenant_id=tenant.id, batch_type=ImportBatchType.ROSTER, trigger=ImportTrigger.INITIAL_ONBOARDING, stage="S0", status=ImportStatus.UPLOADING, submitted_by=lab_admin.id)
    session.add(batch)
    session.flush()

    if registration.training_pooling_consent:
        # Routed through the event log rather than set directly, so the append-only history is complete from the first moment.
        record_consent_event(
            session,
            tenant_id=tenant.id,
            event=TrainingConsentEvent.GRANTED,
            ref=registration.training_consent_ref,
            actor_id=actor_id,
            rederive=False,  # no recordings yet
        )

    session.add(AuditLog(tenant_id=tenant.id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="tenant_registered", entity_type="tenant", entity_id=tenant.id, after={"slug": tenant.slug, "training_pooling_consent": registration.training_pooling_consent, "patient_notice_version": registration.patient_notice_version}))

    transition_status(session, tenant.id, TenantStatus.ONBOARDING, actor_id=actor_id)

    log.info("lab_registered", tenant_id=str(tenant.id), slug=tenant.slug, pooling_consent=registration.training_pooling_consent)
    return RegistrationResult(tenant=tenant, lab_admin=lab_admin, acceptance_eval_set=acceptance_set, onboarding_batch=batch)


def transition_status(session: Session, tenant_id: uuid.UUID, target: str, *, actor_id: uuid.UUID | None = None) -> Tenant:
    """Move a tenant through its lifecycle, enforcing the gates."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"no tenant {tenant_id}")

    previous = tenant.status
    s7_passed: bool | None = None
    if (previous, target) == (TenantStatus.ONBOARDING, TenantStatus.PILOT):
        s7_passed = evaluate_readiness(session, tenant_id).passed

    assert_transition_allowed(previous, target, s7_readiness_passed=s7_passed)

    tenant.status = target
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="tenant_status_changed", entity_type="tenant", entity_id=tenant_id, before={"status": previous}, after={"status": target, "s7_readiness_passed": s7_passed}))
    session.flush()

    if target == TenantStatus.SUSPENDED:
        # `suspended` routes all reports to the manual fallback path rather than failing them.
        log.warning("tenant_suspended_manual_fallback", tenant_id=str(tenant_id), detail="incoming recordings must route to the documented degraded mode")

    log.info("tenant_status_changed", tenant_id=str(tenant_id), previous=previous, target=target, s7_readiness_passed=s7_passed)
    return tenant


def offboard(session: Session, tenant_id: uuid.UUID, *, actor_id: uuid.UUID | None = None) -> Tenant:
    """Terminal transition. Export-then-purge is **not** implemented here."""
    tenant = transition_status(session, tenant_id, TenantStatus.OFFBOARDED, actor_id=actor_id)
    log.warning("tenant_offboarded", tenant_id=str(tenant_id), detail="export-then-purge cascade is not implemented;")
    return tenant
