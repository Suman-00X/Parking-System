"""The one endpoint that accepts a recording: validate it, store the audio, write the row, record the audit entry.

Order: upload_recording does all four and returns an IngestResponse. It only captures; nothing
is transcribed here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from radreport.adapters.storage.object_store import S3ObjectStore
from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.core.config import get_settings
from radreport.core.errors import DuplicateRecording, IngestRejected
from radreport.core.types import CaptureDeviceClass
from radreport.ingest.service import IngestRequest, ingest_recording

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


@router.post("/recordings", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def upload_recording(session: DbSession, principal: CurrentPrincipal, file: Annotated[UploadFile, File()], study_id: Annotated[uuid.UUID, Form()], radiologist_id: Annotated[uuid.UUID, Form()], device_id: Annotated[str | None, Form()] = None, capture_device_class: Annotated[str, Form()] = CaptureDeviceClass.DICTATION_MIC_PTT, is_push_to_talk: Annotated[bool, Form()] = True) -> IngestResponse:
    if principal.tenant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no tenant bound to this request")

    settings = get_settings()
    data = await file.read()

    request = IngestRequest(tenant_id=principal.tenant_id, study_id=study_id, radiologist_id=radiologist_id, filename=file.filename or "recording", data=data, device_id=device_id, capture_device_class=capture_device_class, is_push_to_talk=is_push_to_talk, actor_id=principal.id)

    store = S3ObjectStore(settings.storage)
    try:
        result = ingest_recording(session, store, request)
    except DuplicateRecording as exc:
        # Idempotent, not an error.
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"message": "this audio has already been ingested", "recording_id": exc.recording_id, "content_hash": exc.content_hash}) from exc
    except IngestRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"message": exc.reason, "code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    recording = result.recording
    return IngestResponse(recording_id=recording.id, content_hash=recording.content_hash, duration_seconds=float(recording.duration_seconds or 0), sample_rate_hz=recording.sample_rate_hz or 0, audio_format=recording.audio_format, measured_snr_db=(float(recording.measured_snr_db) if recording.measured_snr_db is not None else None), silence_ratio=(float(recording.silence_ratio) if recording.silence_ratio is not None else None), capture_device_class=recording.capture_device_class, warnings=result.probe.warnings)
