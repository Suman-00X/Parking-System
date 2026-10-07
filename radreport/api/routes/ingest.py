"""The one endpoint that accepts a recording: validate it, store the audio, write the row, record the audit entry.

Order: upload_recording does all four, queues a run_pipeline job in the same transaction, and
returns an IngestResponse. Nothing is transcribed in the request; a worker does that. list_recordings pages through what a lab has captured.
register_study_route records the study (and patient) a recording belongs to, when no hospital system sends it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from radreport.adapters.storage.object_store import StorageUnavailable, object_store
from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.api.pagination import Page, paginate_async, set_page_headers
from radreport.core.config import get_settings
from radreport.core.errors import DuplicateRecording, IngestRejected
from radreport.core.types import CaptureDeviceClass
from radreport.db.async_session import async_read_session
from radreport.db.models.ingestion import Recording
from radreport.events.outbox import Topic, emit
from radreport.ingest.service import IngestRequest, ingest_recording
from radreport.ingest.studies import StudyIn, register_study
from radreport.workers.queue import enqueue

router = APIRouter(prefix="/ingest", tags=["ingest"])


class IngestResponse(BaseModel):
    recording_id: uuid.UUID
    content_hash: str
    duration_seconds: float
    sample_rate_hz: int
    audio_format: str
    measured_snr_db: float | None
    silence_ratio: float | None
    capture_device_class: str
    warnings: list[str]
    """Warn-level gate results."""

    pipeline_job_id: uuid.UUID | None = None
    """The queued run_pipeline job; a worker picks it up, so the upload returns without waiting for transcription."""


@router.post("/recordings", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def upload_recording(session: DbSession, principal: CurrentPrincipal, file: Annotated[UploadFile, File()], study_id: Annotated[uuid.UUID, Form()], radiologist_id: Annotated[uuid.UUID, Form()], device_id: Annotated[str | None, Form()] = None, capture_device_class: Annotated[str, Form()] = CaptureDeviceClass.DICTATION_MIC_PTT, is_push_to_talk: Annotated[bool, Form()] = True) -> IngestResponse:
    if principal.tenant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no tenant bound to this request")

    settings = get_settings()
    data = await file.read()

    request = IngestRequest(tenant_id=principal.tenant_id, study_id=study_id, radiologist_id=radiologist_id, filename=file.filename or "recording", data=data, device_id=device_id, capture_device_class=capture_device_class, is_push_to_talk=is_push_to_talk, actor_id=principal.id)

    store = object_store(settings.storage)
    try:
        result = ingest_recording(session, store, request)
    except DuplicateRecording as exc:
        # Idempotent, not an error.
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"message": "this audio has already been ingested", "recording_id": exc.recording_id, "content_hash": exc.content_hash}) from exc
    except IngestRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"message": exc.reason, "code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except StorageUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    recording = result.recording
    # Queued in the ingest transaction: the job exists exactly when the recording does, and a retried upload queues nothing new.
    pipeline_job = enqueue(session, "run_pipeline", {"recording_id": str(recording.id)}, tenant_id=principal.tenant_id, dedupe_key=f"recording:{recording.id}")
    emit(session, Topic.RECORDING_INGESTED, {"recording_id": recording.id, "study_id": recording.study_id, "radiologist_id": recording.radiologist_id, "capture_device_class": recording.capture_device_class}, tenant_id=principal.tenant_id)
    return IngestResponse(pipeline_job_id=pipeline_job, recording_id=recording.id, content_hash=recording.content_hash, duration_seconds=float(recording.duration_seconds or 0), sample_rate_hz=recording.sample_rate_hz or 0, audio_format=recording.audio_format, measured_snr_db=(float(recording.measured_snr_db) if recording.measured_snr_db is not None else None), silence_ratio=(float(recording.silence_ratio) if recording.silence_ratio is not None else None), capture_device_class=recording.capture_device_class, warnings=result.probe.warnings)


class RecordingSummary(BaseModel):
    recording_id: uuid.UUID
    study_id: uuid.UUID
    radiologist_id: uuid.UUID
    uploaded_at: str
    duration_seconds: float | None
    audio_format: str
    capture_device_class: str
    measured_snr_db: float | None


@router.get("/recordings", response_model=list[RecordingSummary])
async def list_recordings(principal: CurrentPrincipal, response: Response, radiologist_id: uuid.UUID | None = None, page: int | None = None, page_size: int | None = None) -> list[RecordingSummary]:
    """The lab's recordings, newest first, a page at a time. On the async driver: it runs on the event loop, not a worker thread."""
    query = select(Recording).where(Recording.tenant_id == principal.tenant_id)
    if radiologist_id is not None:
        query = query.where(Recording.radiologist_id == radiologist_id)
    async with async_read_session(principal.tenant_id, principal=principal) as session:
        paged = await paginate_async(session, query.order_by(Recording.uploaded_at.desc(), Recording.id), Page.of(page, page_size))
    set_page_headers(response, paged, "/ingest/recordings", {"radiologist_id": str(radiologist_id)} if radiologist_id else None)
    return [RecordingSummary(recording_id=r.id, study_id=r.study_id, radiologist_id=r.radiologist_id, uploaded_at=r.uploaded_at.isoformat(), duration_seconds=float(r.duration_seconds) if r.duration_seconds is not None else None, audio_format=r.audio_format, capture_device_class=r.capture_device_class, measured_snr_db=float(r.measured_snr_db) if r.measured_snr_db is not None else None) for r in paged.rows]


class StudyBody(BaseModel):
    mrn: str = Field(min_length=1, max_length=64)
    accession_number: str = Field(min_length=1, max_length=64)
    modality: str | None = Field(default=None, max_length=16)
    body_part_examined: str | None = Field(default=None, max_length=64)
    study_description: str | None = Field(default=None, max_length=200)
    referring_doctor: str | None = Field(default=None, max_length=120)
    priority: str = Field(default="routine", pattern="^(routine|urgent|stat)$")
    sex: str | None = Field(default=None, pattern="^[MFO]$")
    age_years: int | None = Field(default=None, ge=0, le=130)


@router.post("/studies", status_code=status.HTTP_201_CREATED)
def register_study_route(body: StudyBody, session: DbSession, principal: CurrentPrincipal, response: Response) -> dict[str, object]:
    """The study for an accession number, created with its patient if new; 200 with the same ids when it already exists."""
    if principal.tenant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no tenant bound to this request")
    result = register_study(session, principal.tenant_id, StudyIn(**body.model_dump()), actor_id=principal.id)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return {"study_id": str(result.study_id), "patient_id": str(result.patient_id), "created": result.created}
