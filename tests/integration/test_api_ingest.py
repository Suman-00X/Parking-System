"""Ingest over real HTTP, through the running application."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.auth.lab import issue_access_token
from radreport.core.types import UserRole
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio, synth_lossy_audio, synth_patient_fields

pytestmark = pytest.mark.db


@pytest.fixture
def client(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app, with object storage swapped for an in-memory store."""
    from radreport.api.routes import ingest as ingest_route

    store = InMemoryObjectStore()
    monkeypatch.setattr(ingest_route, "S3ObjectStore", lambda _settings: store)

    from radreport.api.app import create_app

    app = create_app()
    test_client = TestClient(app)
    test_client.store = store  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def seeded(migrated_db: str, two_tenants):
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"E-{uuid.uuid4().hex[:6]}", display_name="Dr Test", roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()
        radiologist = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
        patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=11))
        session.add_all([radiologist, patient])
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        return {"tenant_id": str(tenant_id), "user_id": str(user.id), "study_id": str(study.id), "radiologist_id": str(radiologist.id)}


def _headers(seeded: dict) -> dict[str, str]:
    token, _ = issue_access_token(user_id=uuid.UUID(seeded["user_id"]), tenant_id=uuid.UUID(seeded["tenant_id"]), roles=[UserRole.RADIOLOGIST])
    return {"Authorization": f"Bearer {token}"}


def _post(client: TestClient, seeded: dict, data: bytes, name: str = "d.flac"):
    return client.post("/ingest/recordings", headers=_headers(seeded), files={"file": (name, data, "audio/flac")}, data={"study_id": seeded["study_id"], "radiologist_id": seeded["radiologist_id"]})


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_upload_returns_the_measured_quality_fields(client: TestClient, seeded: dict) -> None:
    """Warnings are returned, not swallowed — so the uploading client can tell the radiologist their mic is wrong while they are still next to it."""
    response = _post(client, seeded, synth_audio(seconds=30, snr_db=25, silence_ratio=0.05))
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["audio_format"] == "flac"
    assert body["sample_rate_hz"] == 16_000
    assert body["measured_snr_db"] is not None
    assert body["warnings"] == []
    assert client.store.exists  # stored


def test_a_low_quality_upload_is_accepted_with_warnings(client: TestClient, seeded: dict) -> None:
    """Warn-level gates do not reject: the dictation still has clinical value, and rejecting it would lose real work."""
    response = _post(client, seeded, synth_audio(seconds=30, sample_rate=8_000, snr_db=20))
    assert response.status_code == 201
    assert any("sample rate" in w for w in response.json()["warnings"])


def test_a_retried_upload_returns_conflict_with_the_original_id(client: TestClient, seeded: dict) -> None:
    """The idempotency, surfaced so a client can recover without a human."""
    audio = synth_audio(seconds=25, seed=42)
    first = _post(client, seeded, audio)
    assert first.status_code == 201

    retry = _post(client, seeded, audio)
    assert retry.status_code == 409
    assert retry.json()["detail"]["recording_id"] == first.json()["recording_id"]


def test_lossy_upload_is_rejected_with_a_reason(client: TestClient, seeded: dict) -> None:
    response = _post(client, seeded, synth_lossy_audio(), name="d.mp3")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "lossy_codec"


def test_unauthenticated_requests_are_refused(client: TestClient, seeded: dict) -> None:
    response = client.post("/ingest/recordings", files={"file": ("d.flac", synth_audio(seconds=20), "audio/flac")}, data={"study_id": seeded["study_id"], "radiologist_id": seeded["radiologist_id"]})
    assert response.status_code == 401


def test_a_platform_user_header_grants_nothing(client: TestClient, seeded: dict) -> None:
    """Admin identity comes only from the admin panel's session cookie; the old header is ignored everywhere."""
    response = client.post("/ingest/recordings", headers={"X-Platform-User-Id": str(uuid.uuid4())}, files={"file": ("d.flac", synth_audio(seconds=20), "audio/flac")}, data={"study_id": seeded["study_id"], "radiologist_id": seeded["radiologist_id"]})
    assert response.status_code == 401
    assert client.get("/admin/api/labs", headers={"X-Platform-User-Id": str(uuid.uuid4())}).status_code == 401


def test_the_old_identity_headers_grant_nothing(client: TestClient, seeded: dict) -> None:
    """Lab identity comes only from a signed access token now."""
    response = client.post("/ingest/recordings", headers={"X-User-Id": seeded["user_id"], "X-Tenant-Id": seeded["tenant_id"]}, files={"file": ("d.flac", synth_audio(seconds=20), "audio/flac")}, data={"study_id": seeded["study_id"], "radiologist_id": seeded["radiologist_id"]})
    assert response.status_code == 401
