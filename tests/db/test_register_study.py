"""A lab registers a study by accession number; the same accession twice is the same study, and another lab cannot see it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from radreport.api.app import create_app
from radreport.db.models.identity import Patient, Study
from radreport.db.session import tenant_session
from tests.db.helpers import lab_headers

pytestmark = pytest.mark.db


def test_register_study(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    client = TestClient(create_app())
    headers = lab_headers(migrated_db, lab, "radiologist")
    body = {"mrn": "SYNTH-0001", "accession_number": "ACC-1", "modality": "CT", "body_part_examined": "chest", "sex": "F", "age_years": 54}
    first = client.post("/ingest/studies", json=body, headers=headers)
    again = client.post("/ingest/studies", json=body, headers=headers)
    second = client.post("/ingest/studies", json={**body, "accession_number": "ACC-2"}, headers=headers)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["study_id"] == first.json()["study_id"]
    assert second.json()["patient_id"] == first.json()["patient_id"], "one patient per MRN"
    assert client.post("/ingest/studies", json={**body, "name": "Ram Kumar"}, headers=headers).status_code == 400, "no patient name is accepted"
    assert client.post("/ingest/studies", json=body, headers=lab_headers(migrated_db, lab, "auditor")).status_code == 403
    with tenant_session(lab, url=migrated_db) as session:
        patient = session.execute(select(Patient)).scalar_one()
        assert patient.pseudonym.startswith("PT-") and patient.mrn == "SYNTH-0001"
    with tenant_session(other, url=migrated_db) as session:
        assert session.execute(select(func.count()).select_from(Study)).scalar_one() == 0
