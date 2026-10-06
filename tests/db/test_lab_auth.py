"""Lab users signing in, refreshing, signing out and changing passwords, against a real database."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from radreport.admin import auth as admin_auth
from radreport.api.app import create_app
from radreport.auth import lab
from radreport.core.types import PlatformRole, TenantStatus, UserRole
from radreport.db.models.identity import AppUser, LabRefreshToken
from radreport.db.models.tenancy import PlatformUser, Tenant
from radreport.db.session import system_session, tenant_session

pytestmark = pytest.mark.db

PASSWORD = "correct horse battery staple"


@pytest.fixture
def lab_user(migrated_db: str, two_tenants):
    """A radiologist with a password, in a lab with a slug."""
    tenant_id, other_id = two_tenants
    with system_session(migrated_db) as session:
        slug = session.get(Tenant, tenant_id).slug
        other_slug = session.get(Tenant, other_id).slug
    email = f"dr-{uuid.uuid4().hex[:8]}@lab.example"
    with tenant_session(tenant_id, url=migrated_db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Lab", email=email, roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()
        lab.set_password(session, user_id=user.id, password=PASSWORD, actor_id=None)
        user_id = user.id
    return {"db": migrated_db, "tenant_id": tenant_id, "slug": slug, "other_slug": other_slug, "email": email, "user_id": user_id}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), follow_redirects=False)


def _login(client: TestClient, f: dict, **overrides: str):
    return client.post("/auth/login", json={"lab": f["slug"], "email": f["email"], "password": PASSWORD} | overrides)


def test_signing_in_returns_tokens_that_open_lab_routes(client: TestClient, lab_user) -> None:
    response = _login(client, lab_user, email=lab_user["email"].upper())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert client.get("/review/queue", headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 200


@pytest.mark.parametrize("overrides", [{"password": "wrong password here"}, {"email": "nobody@lab.example"}, {"lab": "no-such-lab"}])
def test_every_failed_sign_in_looks_the_same(client: TestClient, lab_user, overrides: dict[str, str]) -> None:
    response = _login(client, lab_user, **overrides)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid lab, email or password"


def test_a_user_cannot_sign_in_through_another_lab(client: TestClient, lab_user) -> None:
    assert _login(client, lab_user, lab=lab_user["other_slug"]).status_code == 401


def test_an_inactive_user_and_an_offboarded_lab_cannot_sign_in(client: TestClient, lab_user) -> None:
    with system_session(lab_user["db"]) as session:
        session.get(Tenant, lab_user["tenant_id"]).status = TenantStatus.OFFBOARDED
    assert _login(client, lab_user).status_code == 401

    with system_session(lab_user["db"]) as session:
        session.get(Tenant, lab_user["tenant_id"]).status = TenantStatus.ONBOARDING
    with tenant_session(lab_user["tenant_id"], url=lab_user["db"]) as session:
        session.get(AppUser, lab_user["user_id"]).is_active = False
    assert _login(client, lab_user).status_code == 401


def test_refresh_rotates_and_a_replayed_token_ends_the_sign_in(client: TestClient, lab_user) -> None:
    first = _login(client, lab_user).json()["refresh_token"]
    second = client.post("/auth/refresh", json={"refresh_token": first})
    assert second.status_code == 200
    newer = second.json()["refresh_token"]
    assert newer != first

    # The old token again: treated as stolen, so the newer one dies with it.
    assert client.post("/auth/refresh", json={"refresh_token": first}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": newer}).status_code == 401


def test_only_the_refresh_token_hash_is_stored(client: TestClient, lab_user) -> None:
    raw = _login(client, lab_user).json()["refresh_token"]
    with tenant_session(lab_user["tenant_id"], url=lab_user["db"]) as session:
        hashes = [t.token_hash for t in session.query(LabRefreshToken).filter_by(app_user_id=lab_user["user_id"]).all()]
    assert hashes and raw not in hashes and all(raw.split(".")[1] not in h for h in hashes)


def test_logout_ends_the_sign_in(client: TestClient, lab_user) -> None:
    raw = _login(client, lab_user).json()["refresh_token"]
    assert client.post("/auth/logout", json={"refresh_token": raw}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": raw}).status_code == 401


def test_changing_your_password_signs_you_out_everywhere(client: TestClient, lab_user) -> None:
    tokens = _login(client, lab_user).json()
    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.post("/auth/password", headers=bearer, json={"current_password": "wrong one here", "new_password": "a brand new passphrase"}).status_code == 403
    assert client.post("/auth/password", headers=bearer, json={"current_password": PASSWORD, "new_password": "a brand new passphrase"}).status_code == 204

    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert _login(client, lab_user).status_code == 401
    assert _login(client, lab_user, password="a brand new passphrase").status_code == 200


def test_a_product_admin_sets_a_lab_users_password(client: TestClient, lab_user) -> None:
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    with system_session(lab_user["db"]) as session:
        session.add(PlatformUser(email=admin_email, display_name="Admin", role=PlatformRole.PRODUCT_ADMIN))
        session.flush()
        admin_auth.set_password(session, email=admin_email, password=PASSWORD)
    old_refresh = _login(client, lab_user).json()["refresh_token"]

    client.cookies.set(admin_auth.SESSION_COOKIE, client.post("/admin/login", data={"email": admin_email, "password": PASSWORD}).cookies[admin_auth.SESSION_COOKIE])
    listed = client.get(f"/admin/api/labs/{lab_user['tenant_id']}/users").json()
    assert any(u["id"] == str(lab_user["user_id"]) and u["can_sign_in"] for u in listed)

    reset = client.post(f"/admin/api/labs/{lab_user['tenant_id']}/users/{lab_user['user_id']}/password", json={"password": "set by the admin panel"})
    assert reset.status_code == 200
    client.cookies.clear()
    assert client.post("/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401
    assert _login(client, lab_user, password="set by the admin panel").status_code == 200
