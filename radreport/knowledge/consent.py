"""Works out which recordings may be used to train a model, deriving it from the consent record rather than trusting a flag.

Order: judge one recording (evaluate_eligibility) -> set the flag from that judgement
(derive_training_eligibility) -> re-derive a whole lab after a consent change
(rederive_for_tenant, record_consent_event) -> confirm a training set's legal basis
(verify_g6_legal_basis).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.cache.lookups import forget_tenant
from radreport.core.logging import get_logger
from radreport.core.types import TrainingConsentEvent
from radreport.db.models.identity import RadiologistProfile
from radreport.db.models.ingestion import Recording
from radreport.db.models.tenancy import Tenant, TrainingConsentEventLog

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    eligible: bool
    reasons: tuple[str, ...]
    """Why not. Empty when eligible — an auditable answer, not a bare boolean."""


def evaluate_eligibility(recording: Recording, tenant: Tenant, radiologist: RadiologistProfile | None) -> EligibilityVerdict:
    """Pure predicate. No I/O, so it is trivially testable and replayable."""
    reasons: list[str] = []

    if not tenant.training_pooling_consent:
        reasons.append("tenant has no training-pooling consent ( lab agreement clause)")

    effective_from = tenant.consent_effective_from
    withdrawn_at = tenant.consent_withdrawn_at
    uploaded_at = recording.uploaded_at

    if tenant.training_pooling_consent:
        if effective_from is None:
            reasons.append("tenant consent has no effective_from date")
        elif uploaded_at < effective_from:
            reasons.append(f"uploaded {uploaded_at.isoformat()} predates consent effective from {effective_from.isoformat()}")
        if withdrawn_at is not None and uploaded_at >= withdrawn_at:
            reasons.append(f"uploaded {uploaded_at.isoformat()} is at or after consent withdrawal {withdrawn_at.isoformat()}")

    if radiologist is None:
        reasons.append("no radiologist profile on this recording")
    elif not radiologist.training_consent_ref:
        reasons.append("speaker has no training_consent_ref; enrollment consent ( voice_consent_ref) is a different purpose and does not cover training")

    if recording.phi_scrub_completed_at is None:
        reasons.append("audio PHI scrub not completed: spoken patient identifiers would enter the pooled corpus permanently")

    return EligibilityVerdict(eligible=not reasons, reasons=tuple(reasons))


def derive_training_eligibility(session: Session, recording: Recording, *, commit: bool = False) -> EligibilityVerdict:
    """Compute and write `is_training_corpus_eligible` for one recording."""
    tenant = session.get(Tenant, recording.tenant_id)
    if tenant is None:
        raise ValueError(f"no tenant {recording.tenant_id}")
    radiologist = session.get(RadiologistProfile, recording.radiologist_id)

    verdict = evaluate_eligibility(recording, tenant, radiologist)
    if recording.is_training_corpus_eligible != verdict.eligible:
        recording.is_training_corpus_eligible = verdict.eligible
        session.flush()
    if commit:
        session.commit()
    return verdict


def rederive_for_tenant(session: Session, tenant_id: uuid.UUID) -> tuple[int, int]:
    """Re-derive every recording in a tenant. Returns (eligible, ineligible)."""
    recordings = session.execute(select(Recording).where(Recording.tenant_id == tenant_id)).scalars().all()
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"no tenant {tenant_id}")
    # One read of the lab's radiologists and one flush, instead of two reads and a flush per recording.
    radiologists = {p.id: p for p in session.execute(select(RadiologistProfile).where(RadiologistProfile.tenant_id == tenant_id)).scalars().all()}
    eligible = 0
    for recording in recordings:
        verdict = evaluate_eligibility(recording, tenant, radiologists.get(recording.radiologist_id))
        if recording.is_training_corpus_eligible != verdict.eligible:
            recording.is_training_corpus_eligible = verdict.eligible
        eligible += int(verdict.eligible)
    session.flush()

    log.info("training_eligibility_rederived", tenant_id=str(tenant_id), total=len(recordings), eligible=eligible)
    return eligible, len(recordings) - eligible


def record_consent_event(session: Session, *, tenant_id: uuid.UUID, event: str, ref: str | None = None, actor_id: uuid.UUID | None = None, occurred_at: dt.datetime | None = None, rederive: bool = True) -> TrainingConsentEventLog:
    """Append to the consent history and update the tenant's current state."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"no tenant {tenant_id}")

    when = occurred_at or dt.datetime.now(dt.UTC)

    if event == TrainingConsentEvent.GRANTED:
        tenant.training_pooling_consent = True
        tenant.consent_effective_from = when
        tenant.consent_withdrawn_at = None
        tenant.training_consent_ref = ref or tenant.training_consent_ref
    elif event == TrainingConsentEvent.RENEWED:
        tenant.training_pooling_consent = True
        tenant.consent_withdrawn_at = None
        tenant.training_consent_ref = ref or tenant.training_consent_ref
    elif event == TrainingConsentEvent.WITHDRAWN:
        # Deliberately leaves `consent_effective_from` intact.
        tenant.training_pooling_consent = False
        tenant.consent_withdrawn_at = when
    else:
        raise ValueError(f"unknown consent event {event!r}")

    forget_tenant(tenant_id, session)
    entry = TrainingConsentEventLog(tenant_id=tenant_id, event=event, ref=ref, actor_id=actor_id, occurred_at=when)
    session.add(entry)
    session.flush()

    if rederive:
        rederive_for_tenant(session, tenant_id)

    return entry


def verify_g6_legal_basis(session: Session, recording_ids: list[uuid.UUID]) -> tuple[bool, dict[str, list[str]]]:
    """`G6_legal_basis`, strengthened."""
    failures: dict[str, list[str]] = {}
    # Three bulk reads, however many recordings: a training corpus runs to thousands.
    recordings = {r.id: r for r in session.execute(select(Recording).where(Recording.id.in_(recording_ids))).scalars().all()} if recording_ids else {}
    tenants = {t.id: t for t in session.execute(select(Tenant).where(Tenant.id.in_({r.tenant_id for r in recordings.values()}))).scalars().all()} if recordings else {}
    radiologists = {p.id: p for p in session.execute(select(RadiologistProfile).where(RadiologistProfile.id.in_({r.radiologist_id for r in recordings.values()}))).scalars().all()} if recordings else {}
    for recording_id in recording_ids:
        recording = recordings.get(recording_id)
        if recording is None:
            failures[str(recording_id)] = ["recording not found"]
            continue
        tenant = tenants.get(recording.tenant_id)
        verdict = evaluate_eligibility(recording, tenant, radiologists.get(recording.radiologist_id)) if tenant else None
        if verdict is None or not verdict.eligible:
            failures[str(recording_id)] = list(verdict.reasons) if verdict else ["no tenant"]

    return (not failures), failures
