"""Registers the study a dictation belongs to, and its patient, the way a hospital system would send them.

Order: find the patient by MRN or create them with a server-made pseudonym (the only identifier any
prompt sees) -> find the study by accession number or create it (register_study, idempotent: the
same accession twice returns the first study). No patient name is accepted.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.types import ActorType, MetadataSource, StudyPriority
from radreport.db.models.identity import Patient, Study
from radreport.db.models.orchestration import AuditLog


@dataclass(frozen=True, slots=True)
class StudyIn:
    mrn: str
    accession_number: str
    modality: str | None = None
    body_part_examined: str | None = None
    study_description: str | None = None
    referring_doctor: str | None = None
    priority: str = StudyPriority.ROUTINE
    study_datetime: dt.datetime | None = None
    sex: str | None = None
    age_years: int | None = None


@dataclass(frozen=True, slots=True)
class Registered:
    study_id: uuid.UUID
    patient_id: uuid.UUID
    created: bool


def register_study(session: Session, tenant_id: uuid.UUID, data: StudyIn, *, actor_id: uuid.UUID | None) -> Registered:
    """The study for this accession number, created with its patient if new."""
    existing = session.execute(select(Study).where(Study.tenant_id == tenant_id, Study.accession_number == data.accession_number)).scalar_one_or_none()
    if existing is not None:
        return Registered(study_id=existing.id, patient_id=existing.patient_id, created=False)
    patient = session.execute(select(Patient).where(Patient.tenant_id == tenant_id, Patient.mrn == data.mrn)).scalar_one_or_none()
    if patient is None:
        patient = Patient(tenant_id=tenant_id, mrn=data.mrn, pseudonym=f"PT-{secrets.token_hex(5)}", sex=data.sex, age_years=data.age_years)
        session.add(patient)
        session.flush()
    study = Study(tenant_id=tenant_id, patient_id=patient.id, accession_number=data.accession_number, modality=data.modality, body_part_examined=data.body_part_examined, study_description=data.study_description, referring_doctor=data.referring_doctor, priority=data.priority, study_datetime=data.study_datetime or dt.datetime.now(dt.UTC), metadata_source=MetadataSource.UPLOAD)
    session.add(study)
    session.flush()
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER, action="study_registered", entity_type="study", entity_id=study.id, after={"accession_number": data.accession_number, "modality": data.modality}))
    session.flush()
    return Registered(study_id=study.id, patient_id=patient.id, created=True)
