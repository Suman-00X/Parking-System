"""Statements per request stay flat as rows grow: the admin pages and the bulk checks have no per-row queries."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import UserRole
from radreport.db import instrumentation
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session, tenant_session
from tests.conftest import _purge_tenants
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db


def _count(client, path: str) -> int:
    response = client.get(path)
    assert response.status_code == 200, response.text[:300]
    return int(response.headers["x-query-count"])


def _add_staff(db: str, tenant_id: uuid.UUID, n: int) -> None:
    with tenant_session(tenant_id, url=db) as session:
        session.add_all(AppUser(tenant_id=tenant_id, employee_code=f"E-{uuid.uuid4().hex[:8]}", display_name=f"Staff {i}", roles=[UserRole.RADIOLOGIST]) for i in range(n))


def test_the_lab_list_does_not_grow_with_the_number_of_labs(migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    before = _count(client, "/admin/labs")
    extra: list[uuid.UUID] = []
    with system_session(migrated_db) as session:
        for _ in range(10):
            tenant = Tenant(name="Count lab", slug=f"count-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            session.flush()
            extra.append(tenant.id)
    try:
        assert _count(client, "/admin/labs") == before
        assert before < 10, "the lab list should need a handful of statements"
    finally:
        _purge_tenants(migrated_db, extra)


def test_the_lab_page_does_not_grow_with_its_staff(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    _add_staff(migrated_db, tenant_id, 1)
    before = _count(client, f"/admin/labs/{tenant_id}")
    _add_staff(migrated_db, tenant_id, 15)
    assert _count(client, f"/admin/labs/{tenant_id}") == before
    assert _count(client, f"/admin/api/labs/{tenant_id}/users") <= 8


def test_the_users_page_does_not_grow_with_the_number_of_accounts(migrated_db: str) -> None:
    client = signed_in(make_platform_user(migrated_db))
    before = _count(client, "/admin/users")
    for _ in range(5):
        make_platform_user(migrated_db)
    assert _count(client, "/admin/users") == before


def test_onboarding_and_readiness_read_each_fact_once(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    assert _count(client, f"/admin/labs/{tenant_id}/readiness") <= 14
    assert _count(client, f"/admin/labs/{tenant_id}/onboarding") <= 18


def test_the_legal_basis_check_reads_in_bulk(migrated_db: str, two_tenants) -> None:
    """Three reads whatever the corpus size, not three per recording."""
    from radreport.knowledge.consent import verify_g6_legal_basis

    with instrumentation.query_scope("g6") as few, system_session(migrated_db) as session:
        verify_g6_legal_basis(session, [uuid.uuid4()])
    with instrumentation.query_scope("g6") as many, system_session(migrated_db) as session:
        verify_g6_legal_basis(session, [uuid.uuid4() for _ in range(50)])
    assert many.count == few.count
