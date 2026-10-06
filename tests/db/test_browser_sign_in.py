"""A lab user signing in to the review screens from a browser, against a real database."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from radreport.auth import lab
from radreport.auth.lab import ACCESS_COOKIE, REFRESH_COOKIE
from radreport.core.types import UserRole
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session, tenant_session

pytestmark = pytest.mark.db

PASSWORD = "correct horse battery staple"


@pytest.fixture
def browser(migrated_db: str, two_tenants):
    tenant_id, _ = two_tenants
    with system_session(migrated_db) as session:
        slug = session.get(Tenant, tenant_id).slug
    email = f"dr-{uuid.uuid4().hex[:8]}@lab.example"
    with tenant_session(tenant_id, url=migrated_db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Browser", email=email, roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()
        lab.set_password(session, user_id=user.id, password=PASSWORD, actor_id=None)
    client = TestClient(create_app(), follow_redirects=False)
    return client, {"lab": slug, "email": email, "password": PASSWORD}


def test_signing_in_sets_httponly_cookies_and_opens_the_queue(browser) -> None:
    client, form = browser
    response = client.post("/ui/login", data=form | {"next": "/ui/queue"})
    assert response.status_code == 303 and response.headers["location"] == "/ui/queue"
    set_cookies = response.headers.get_list("set-cookie")
    assert all("HttpOnly" in c for c in set_cookies) and len(set_cookies) == 2
    assert any(c.startswith(f"{REFRESH_COOKIE}=") and "Path=/ui" in c for c in set_cookies), "the refresh token goes only to /ui"

    page = client.get("/ui/queue")
    assert page.status_code == 200 and "Review queue" in page.text and "Sign out" in page.text


def test_a_wrong_password_returns_to_the_form(browser) -> None:
    client, form = browser
    response = client.post("/ui/login", data=form | {"password": "wrong password here"})
    assert response.status_code == 303 and response.headers["location"].startswith("/ui/login?error=")


def test_an_expired_access_cookie_is_renewed_silently(browser) -> None:
    client, form = browser
    client.post("/ui/login", data=form)
    client.cookies.delete(ACCESS_COOKIE)
    hop = client.get("/ui/queue")
    assert hop.headers["location"] == "/ui/refresh?next=%2Fui%2Fqueue"
    renewed = client.get(hop.headers["location"])
    assert renewed.status_code == 303 and renewed.headers["location"] == "/ui/queue"
    assert client.get("/ui/queue").status_code == 200


def test_signing_out_ends_the_sign_in(browser) -> None:
    client, form = browser
    client.post("/ui/login", data=form)
    old_refresh = client.cookies.get(REFRESH_COOKIE)
    assert client.post("/ui/logout").headers["location"] == "/ui/login"
    assert client.get("/ui/queue").headers["location"].startswith("/ui/login")
    assert client.post("/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401


def test_the_sign_button_is_not_refused_by_the_input_check(browser) -> None:
    """It used to post a body of {} to a route that takes none."""
    client, form = browser
    client.post("/ui/login", data=form)
    response = client.post(f"/review/drafts/{uuid.uuid4()}/sign", headers={"Origin": "http://testserver"})
    assert response.status_code != 400, response.text
