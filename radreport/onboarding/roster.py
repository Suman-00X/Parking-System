"""Imports a lab's staff list and enrolls each radiologist's voice.

Order: read the HR CSV export (parse_roster_csv) -> create the user rows (import_roster) ->
capture a voiceprint together with its consent (enroll_voice) -> record the separate
training-data consent (record_training_consent).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.errors import ConsentRequired
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, ImportBatchType, ImportStatus, ImportTrigger, UserRole
from radreport.db.models.identity import AppUser, RadiologistProfile
from radreport.db.models.onboarding import ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.onboarding.batches import open_batch, record_counts, transition

log = get_logger(__name__)

#: The CSV columns roster import accepts. `employee_code` and `display_name` are required;
#: everything else has a defensible default.
REQUIRED_COLUMNS: frozenset[str] = frozenset({"employee_code", "display_name"})


@dataclass(frozen=True, slots=True)
class RosterRow:
    """One person from the lab's HR export."""

    employee_code: str
    display_name: str
    email: str | None = None
    roles: tuple[str, ...] = (UserRole.RADIOLOGIST,)
    subspecialty: tuple[str, ...] = ()
    default_language: str = "en-IN"


@dataclass(slots=True)
class RosterImportResult:
    batch: ImportBatch
    created: list[AppUser] = field(default_factory=list)
    updated: list[AppUser] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """`employee_code`s that parsed but were rejected, with the reason logged."""

    profiles_created: list[RadiologistProfile] = field(default_factory=list)


def parse_roster_csv(data: bytes) -> tuple[list[RosterRow], list[str]]:
    """Parse an HR export into rows. Returns `(rows, problems)`."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    problems: list[str] = []

    fieldnames = {name.strip().lower() for name in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - fieldnames
    if missing:
        return [], [f"missing required column(s): {', '.join(sorted(missing))}"]

    rows: list[RosterRow] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(reader, start=2):
        record = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        code = record.get("employee_code", "")
        name = record.get("display_name", "")
        if not code or not name:
            problems.append(f"line {line_no}: employee_code and display_name are both required")
            continue
        if code in seen:
            # Within-file duplicates are a data-entry error, not a re-import.
            problems.append(f"line {line_no}: duplicate employee_code {code!r} in this file")
            continue
        seen.add(code)

        roles = _parse_roles(record.get("roles", ""), line_no, problems)
        if roles is None:
            continue

        rows.append(RosterRow(employee_code=code, display_name=name, email=record.get("email") or None, roles=roles, subspecialty=tuple(s.strip() for s in record.get("subspecialty", "").split(";") if s.strip()), default_language=record.get("default_language") or "en-IN"))

    return rows, problems


def _parse_roles(raw: str, line_no: int, problems: list[str]) -> tuple[str, ...] | None:
    """Roles are additive and validated against `UserRole`."""
    if not raw.strip():
        return (UserRole.RADIOLOGIST,)

    valid = set(UserRole.values())
    roles = tuple(r.strip().lower() for r in raw.split(";") if r.strip())
    unknown = [r for r in roles if r not in valid]
    if unknown:
        problems.append(f"line {line_no}: unknown role(s) {', '.join(unknown)}; valid roles are {', '.join(sorted(valid))}")
        return None
    return roles or (UserRole.RADIOLOGIST,)


def import_roster(session: Session, *, tenant_id: uuid.UUID, rows: list[RosterRow], submitted_by: uuid.UUID | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING, batch: ImportBatch | None = None) -> RosterImportResult:
    """Create or update `app_user` rows, and a profile for every radiologist."""
    batch = batch or open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.ROSTER, stage="S0", trigger=trigger, submitted_by=submitted_by)
    if batch.status == ImportStatus.UPLOADING:
        transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)

    result = RosterImportResult(batch=batch)
    batch.item_count += len(rows)

    for row in rows:
        existing = session.execute(select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.employee_code == row.employee_code)).scalar_one_or_none()

        if existing is not None:
            existing.display_name = row.display_name
            if row.email:
                existing.email = row.email
            # Additive: a re-import must not silently strip a role an
            # admin granted after the first import.
            existing.roles = sorted(set(existing.roles) | set(row.roles))
            existing.is_active = True
            result.updated.append(existing)
            user = existing
        else:
            user = AppUser(tenant_id=tenant_id, employee_code=row.employee_code, display_name=row.display_name, email=row.email, roles=list(row.roles))
            session.add(user)
            session.flush()
            result.created.append(user)

        if UserRole.RADIOLOGIST in row.roles:
            profile = session.execute(select(RadiologistProfile).where(RadiologistProfile.tenant_id == tenant_id, RadiologistProfile.user_id == user.id)).scalar_one_or_none()
            if profile is None:
                profile = RadiologistProfile(tenant_id=tenant_id, user_id=user.id, default_language=row.default_language, subspecialty=list(row.subspecialty) or None)
                session.add(profile)
                session.flush()
                result.profiles_created.append(profile)
            elif row.subspecialty:
                profile.subspecialty = list(row.subspecialty)

    session.flush()
    record_counts(session, batch, accepted=len(result.created) + len(result.updated), rejected=0)
    log.info("s0_roster_imported", tenant_id=str(tenant_id), batch_id=str(batch.id), created=len(result.created), updated=len(result.updated), profiles_created=len(result.profiles_created))
    return result


def enroll_voice(session: Session, *, tenant_id: uuid.UUID, radiologist_id: uuid.UUID, embedding: list[float], consent_ref: str, actor_id: uuid.UUID | None = None) -> RadiologistProfile:
    """Store a speaker voiceprint, **with the consent that permits it**."""
    if not consent_ref or not consent_ref.strip():
        raise ConsentRequired("voice enrollment requires a signed consent reference; a voiceprint is biometric data under DPDP")

    profile = session.get(RadiologistProfile, radiologist_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise ValueError(f"no radiologist_profile {radiologist_id} in this tenant")

    profile.voice_embedding = embedding
    profile.voice_consent_ref = consent_ref.strip()
    profile.voice_enrolled_at = dt.datetime.now(dt.UTC)

    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="voice_enrolled", entity_type="radiologist_profile", entity_id=profile.id, after={"voice_consent_ref": profile.voice_consent_ref}))
    session.flush()
    log.info("s0_voice_enrolled", tenant_id=str(tenant_id), radiologist_id=str(profile.id))
    return profile


def record_training_consent(session: Session, *, tenant_id: uuid.UUID, radiologist_id: uuid.UUID, consent_ref: str | None, actor_id: uuid.UUID | None = None) -> RadiologistProfile:
    """The **second** consent: this speaker's audio may train a pooled model."""
    profile = session.get(RadiologistProfile, radiologist_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise ValueError(f"no radiologist_profile {radiologist_id} in this tenant")

    previous = profile.training_consent_ref
    profile.training_consent_ref = consent_ref.strip() if consent_ref else None

    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="training_consent_recorded", entity_type="radiologist_profile", entity_id=profile.id, before={"training_consent_ref": previous}, after={"training_consent_ref": profile.training_consent_ref}))
    session.flush()
    log.info("s0_training_consent_recorded", tenant_id=str(tenant_id), radiologist_id=str(profile.id), granted=profile.training_consent_ref is not None)
    return profile
