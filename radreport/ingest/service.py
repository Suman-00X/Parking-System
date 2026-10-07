"""The capture path: validate the upload, convert it to lossless audio, store it, and write the recording row.

Order: ingest_recording runs all four steps and returns an IngestResult.
force_legacy_device_class stamps archive uploads so they never count as current hardware.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from radreport.adapters.storage.object_store import ObjectStore, audio_key
from radreport.cache import filters
from radreport.core.config import AudioGateSettings, get_settings
from radreport.core.errors import DuplicateRecording
from radreport.core.hashing import hash_bytes
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, CaptureDeviceClass
from radreport.db.models.identity import RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.orchestration import AuditLog
from radreport.ingest.audio_gates import AudioProbe, probe_audio

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestRequest:
    tenant_id: uuid.UUID
    study_id: uuid.UUID
    radiologist_id: uuid.UUID
    filename: str
    data: bytes
    device_id: str | None = None
    capture_device_class: str = CaptureDeviceClass.DICTATION_MIC_PTT
    is_push_to_talk: bool = True
    actor_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    recording: Recording
    probe: AudioProbe
    was_duplicate: bool = False


def ingest_recording(session: Session, store: ObjectStore, request: IngestRequest, *, gates: AudioGateSettings | None = None) -> IngestResult:
    """Validate, store, and record one dictation."""
    settings = get_settings()
    gate_settings = gates or settings.audio

    content_hash = hash_bytes(request.data)

    # The filter only says "definitely new" or "maybe seen"; a maybe is checked against the database.
    if filters.maybe_seen_recording(session, request.tenant_id, content_hash):
        _raise_if_duplicate(session, request.tenant_id, content_hash)

    probe = probe_audio(request.data, gate_settings)

    # Both parents must belong to this tenant.
    _assert_same_tenant(session, request)

    recording_id = uuid.uuid4()
    uploaded_at = dt.datetime.now(dt.UTC)
    key = audio_key(request.tenant_id, recording_id, probe.audio_format, uploaded_at=uploaded_at)
    store.put(key, request.data, content_type=f"audio/{probe.audio_format}")

    recording = Recording(
        id=recording_id,
        tenant_id=request.tenant_id,
        study_id=request.study_id,
        radiologist_id=request.radiologist_id,
        object_key=key,
        content_hash=content_hash,
        device_id=request.device_id,
        capture_device_class=request.capture_device_class,
        is_push_to_talk=request.is_push_to_talk,
        uploaded_at=uploaded_at,
        # eligibility is **derived**, never set at ingest.
        is_training_corpus_eligible=False,
        **probe.as_recording_fields(),
    )
    try:
        # A savepoint, because another worker may have stored the same audio since this worker's filter was built.
        with session.begin_nested():
            session.add(recording)
            session.flush()
    except IntegrityError:
        _raise_if_duplicate(session, request.tenant_id, content_hash)
        raise
    filters.remember_recording(request.tenant_id, content_hash)

    session.add(AuditLog(tenant_id=request.tenant_id, actor_id=request.actor_id, actor_type=ActorType.USER if request.actor_id else ActorType.SYSTEM, action="recording_ingested", entity_type="recording", entity_id=recording.id, after={"object_key": key, "content_hash": content_hash, "capture_device_class": request.capture_device_class, "warnings": probe.warnings}))
    session.flush()

    if probe.warnings:
        log.warning("ingest_quality_warnings", recording_id=str(recording.id), warnings=probe.warnings, snr_db=probe.measured_snr_db, silence_ratio=probe.silence_ratio)

    return IngestResult(recording=recording, probe=probe)


def _raise_if_duplicate(session: Session, tenant_id: uuid.UUID, content_hash: str) -> None:
    existing = session.execute(select(Recording).where(Recording.tenant_id == tenant_id, Recording.content_hash == content_hash)).scalar_one_or_none()
    if existing is not None:
        log.info("ingest_duplicate_ignored", tenant_id=str(tenant_id), recording_id=str(existing.id), content_hash=content_hash[:12])
        raise DuplicateRecording(content_hash, str(existing.id))


def _assert_same_tenant(session: Session, request: IngestRequest) -> None:
    study = session.get(Study, request.study_id)
    if study is None or study.tenant_id != request.tenant_id:
        raise ValueError(f"study {request.study_id} is not in tenant {request.tenant_id}")

    radiologist = session.get(RadiologistProfile, request.radiologist_id)
    if radiologist is None or radiologist.tenant_id != request.tenant_id:
        raise ValueError(f"radiologist {request.radiologist_id} is not in tenant {request.tenant_id}")


def force_legacy_device_class(request: IngestRequest) -> IngestRequest:
    """Stamp `legacy` on lab-admin archive uploads."""
    import dataclasses

    return dataclasses.replace(request, capture_device_class=CaptureDeviceClass.LEGACY, is_push_to_talk=False)
