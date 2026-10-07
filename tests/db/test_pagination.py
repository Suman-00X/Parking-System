"""Every list endpoint pages: bounded rows per response, totals and links in headers, out-of-range pages refused."""

from __future__ import annotations

import uuid

import pytest

from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session
from tests.conftest import _purge_tenants
from tests.db.helpers import lab_headers, make_platform_user, signed_in

pytestmark = pytest.mark.db


@pytest.fixture
def many_labs(migrated_db: str):
    ids: list[uuid.UUID] = []
    with system_session(migrated_db) as session:
        for n in range(5):
            tenant = Tenant(name=f"Paged lab {n}", slug=f"paged-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            session.flush()
            ids.append(tenant.id)
    yield ids
    _purge_tenants(migrated_db, ids)


def test_the_lab_list_pages_with_totals_and_links(migrated_db: str, many_labs) -> None:
    client = signed_in(make_platform_user(migrated_db))
    first = client.get("/admin/api/labs", params={"page_size": 2})
    assert first.status_code == 200
    assert len(first.json()) == 2
    total = int(first.headers["x-total-count"])
    assert total >= 5
    assert 'rel="next"' in first.headers["link"]
    second = client.get("/admin/api/labs", params={"page_size": 2, "page": 2})
    assert {lab["id"] for lab in first.json()}.isdisjoint({lab["id"] for lab in second.json()})
    assert 'rel="prev"' in second.headers["link"]


def test_the_default_page_is_twenty_and_the_maximum_is_one_hundred(migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    assert client.get("/admin/api/users").headers["x-page-size"] == "20"
    assert client.get("/admin/api/users", params={"page_size": 101}).status_code == 400
    assert client.get("/admin/api/users", params={"page": 0}).status_code == 400
    assert client.get("/admin/api/users", params={"page": "x"}).status_code == 400


def test_html_lists_page_too(migrated_db: str, many_labs) -> None:
    client = signed_in(make_platform_user(migrated_db))
    assert client.get("/admin/labs", params={"page": 1}).status_code == 200
    assert client.get("/admin/users", params={"page": 2}).status_code == 200


def test_a_batch_has_a_status_endpoint(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    upload = client.post(f"/admin/api/labs/{tenant_id}/onboarding/roster", files={"file": ("roster.csv", b"employee_code,display_name,roles\nR1,Dr One,radiologist\n", "text/csv")})
    assert upload.status_code in (200, 201), upload.text
    listing = client.get(f"/admin/api/labs/{tenant_id}/onboarding/batches", params={"batch_type": "roster"})
    assert listing.status_code == 200 and listing.headers["x-total-count"] == "1"
    batch_id = listing.json()[0]["id"]
    status = client.get(f"/admin/api/labs/{tenant_id}/onboarding/batches/{batch_id}").json()
    assert status["batch_type"] == "roster" and status["accepted"] == 1
    assert client.get(f"/admin/api/labs/{tenant_id}/onboarding/batches/{uuid.uuid4()}").status_code == 404


def test_a_lab_lists_its_recordings_a_page_at_a_time(migrated_db: str, two_tenants) -> None:
    from fastapi.testclient import TestClient

    from radreport.api.app import create_app

    tenant_id, _ = two_tenants
    response = TestClient(create_app()).get("/ingest/recordings", params={"page_size": 5}, headers=lab_headers(migrated_db, tenant_id, "radiologist"))
    assert response.status_code == 200 and response.json() == []
    assert response.headers["x-total-count"] == "0"
