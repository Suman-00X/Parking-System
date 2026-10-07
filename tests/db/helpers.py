"""Small helpers the DB-backed tests share: a platform account with a password, and a signed-in panel client."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from radreport.admin import auth
from radreport.api.app import create_app
from radreport.core.types import PlatformRole
from radreport.db.models.tenancy import PlatformUser
from radreport.db.session import system_session

PASSWORD = "correct horse battery staple"


def make_platform_user(db: str, *, role: str = PlatformRole.PRODUCT_ADMIN) -> str:
    """Create an admin-panel account with PASSWORD and return its email."""
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    with system_session(db) as session:
        session.add(PlatformUser(email=email, display_name=f"Test {role}", role=role))
        session.flush()
        auth.set_password(session, email=email, password=PASSWORD)
    return email


def signed_in(email: str) -> TestClient:
    """A test client carrying that account's admin session cookie."""
    client = TestClient(create_app(), follow_redirects=False)
    cookie = client.post("/admin/login", data={"email": email, "password": PASSWORD}).cookies.get(auth.SESSION_COOKIE)
    assert cookie, "sign-in failed"
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    return client


def lab_headers(db: str, tenant_id: uuid.UUID, *roles: str) -> dict[str, str]:
    """A lab user with `roles` in that lab, signed in; returns the bearer header."""
    from radreport.auth import lab
    from radreport.db.models.identity import AppUser
    from radreport.db.models.tenancy import Tenant
    from radreport.db.session import tenant_session

    email = f"user-{uuid.uuid4().hex[:8]}@lab.example"
    with system_session(db) as session:
        slug = session.get(Tenant, tenant_id).slug
    with tenant_session(tenant_id, url=db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"U-{uuid.uuid4().hex[:6]}", display_name="Lab User", email=email, roles=list(roles or ("radiologist",)))
        session.add(user)
        session.flush()
        lab.set_password(session, user_id=user.id, password=PASSWORD, actor_id=None)
    token = TestClient(create_app()).post("/auth/login", json={"lab": slug, "email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
