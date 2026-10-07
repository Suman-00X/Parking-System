"""Every screen renders for the roles allowed to open it, carries the responsive frame, and uses only styled classes; public pages need no sign-in."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from radreport.core.config import get_settings
from radreport.db.session import tenant_session
from tests.db.helpers import lab_headers, make_platform_user, signed_in
from tests.db.review_factory import build_signed_report

pytestmark = pytest.mark.db

STATIC = Path("radreport/api/static")
CSS = (STATIC / "app.css").read_text() + (STATIC / "review.css").read_text()
DEFINED = set(re.findall(r"\.([a-zA-Z][\w-]*)", re.sub(r"url\([^)]*\)|\d+\.\d+", "", CSS)))
#: Classes that are hooks for scripts or state, not styling.
UNSTYLED_OK = {"fade-in", "num", "right", "nowrap", "here", "task", "language-python", "language-bash", "language-sh", "language-json", "language-xml", "language-text", "anchor"}


class _Classes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.classes: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "class" and value:
                self.classes.update(value.split())


def _check_frame(path: str, html: str) -> None:
    assert html.lstrip().lower().startswith("<!doctype html>"), path
    assert '<meta name="viewport" content="width=device-width, initial-scale=1' in html, f"{path}: not responsive"
    assert re.search(r"<title>[^<]+ · radreport[^<]*</title>", html), f"{path}: no title"
    assert re.search(r'href="/ui/static/app\.css\?v=[0-9a-f]+"', html), f"{path}: unversioned or missing stylesheet"
    assert "data-theme-toggle" in html, f"{path}: no light/dark toggle"
    assert not re.search(r"\{[a-z_]+\(|\bNone\b</|Traceback", html), f"{path}: template text leaked into the page"
    parser = _Classes()
    parser.feed(html)
    unstyled = {c for c in parser.classes - DEFINED - UNSTYLED_OK if not c.startswith(("tone-", "toc-"))}
    assert not unstyled, f"{path}: classes with no style rule: {sorted(unstyled)}"


@pytest.fixture
def seeded(migrated_db: str, two_tenants):  # type: ignore[no-untyped-def]
    lab, _ = two_tenants
    with tenant_session(lab, url=migrated_db) as session:
        ids = build_signed_report(session, lab)
    return {"lab": lab, **ids}


def test_admin_screens(migrated_db: str, seeded) -> None:
    admin = signed_in(make_platform_user(migrated_db))
    support = signed_in(make_platform_user(migrated_db, role="support"))
    lab = seeded["lab"]
    pages = ["/admin/labs", f"/admin/labs/{lab}", f"/admin/labs/{lab}/readiness", f"/admin/labs/{lab}/onboarding", "/admin/providers", "/admin/users", "/admin/account", "/admin/costs", "/admin/pools", "/admin/config"]
    for path in pages:
        for client in (admin, support):
            response = client.get(path)
            assert response.status_code == 200, f"{path}: {response.status_code}"
            _check_frame(path, response.text)
            assert 'class="sidebar' in response.text or "sidebar" in response.text, f"{path}: no navigation"


def test_lab_screens(migrated_db: str, seeded) -> None:
    client = TestClient(create_app())
    lab = seeded["lab"]
    for role, pages in {"radiologist": ["/ui/queue", "/ui/lexicon", f"/ui/drafts/{seeded['draft_id']}"], "auditor": ["/ui/queue", f"/ui/drafts/{seeded['draft_id']}"], "lab_admin": ["/ui/lexicon"]}.items():
        headers = lab_headers(migrated_db, lab, role)
        for path in pages:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, f"{role} {path}: {response.status_code}"
            _check_frame(path, response.text)
    assert client.get("/ui/lexicon", headers=lab_headers(migrated_db, lab, "transcriptionist")).status_code == 403


def test_a_read_only_role_sees_the_controls_it_cannot_use_disabled(migrated_db: str, seeded) -> None:
    client = TestClient(create_app())
    draft = f"/ui/drafts/{seeded['draft_id']}"
    auditor = client.get(draft, headers=lab_headers(migrated_db, seeded["lab"], "auditor")).text
    assert 'id="save" class="primary" disabled' in auditor and "(read-only)" in auditor
    assert 'id="useless" class="ghost" disabled' in auditor
    radiologist = client.get(draft, headers=lab_headers(migrated_db, seeded["lab"], "radiologist")).text
    assert 'id="save" class="primary" >' in radiologist and 'id="useless" class="ghost" >' in radiologist


def test_public_pages_and_sign_in_pages(migrated_db: str) -> None:
    client = TestClient(create_app())
    for path, active in (("/features", "Features"), ("/demo", "Try the demo"), ("/api-docs", "API docs"), ("/recruiter", "Recruiter tour")):
        response = client.get(path)
        assert response.status_code == 200, path
        _check_frame(path, response.text)
        assert f'aria-current="page">{active}</a>' in response.text, f"{path}: the current page is not marked"
        assert 'class="public-menu-toggle"' in response.text, f"{path}: no phone menu"
    assert 'id="phase-0--is-the-service-up"' in client.get("/api-docs").text, "headings carry the anchors the docs link to"
    features = client.get("/features").text
    assert 'role="tab"' not in features
    assert features.count("data-dialog-open=") == features.count("<dialog") > 20, "every feature carries a Reason dialog"
    recruiter = client.get("/recruiter").text
    assert recruiter.count('role="tab"') == 4 and 'data-key="demo"' in recruiter and 'data-key="in-action"' in recruiter
    assert '<svg viewBox="0 0 1100 580"' in recruiter, "the system-design tab draws the architecture"
    assert "HTTP routes" in recruiter and "test functions" in recruiter
    for path in ("/admin/login", "/ui/login"):
        page = client.get(path).text
        _check_frame(path, page)
        assert 'href="/features"' in page and 'href="/recruiter"' in page, f"{path}: no links to the public pages"


def test_only_read_only_demo_accounts_are_ever_shown(migrated_db: str, monkeypatch) -> None:
    accounts = [{"label": "Read-only admin", "email": "support-demo@x.local", "password": "pw-support", "role": "support"}, {"label": "Lab auditor", "email": "auditor-demo@x.local", "password": "pw-auditor", "role": "auditor", "lab": "sunrise"}, {"label": "Should never show", "email": "admin-demo@x.local", "password": "pw-admin", "role": "product_admin"}, {"label": "Nor this", "email": "rad-demo@x.local", "password": "pw-rad", "role": "radiologist", "lab": "sunrise"}]
    monkeypatch.setenv("RADREPORT_DEMO_ACCOUNTS", json.dumps(accounts))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        admin_login, lab_login, recruiter, demo = client.get("/admin/login").text, client.get("/ui/login").text, client.get("/recruiter").text, client.get("/demo").text
        assert "Test credentials" in admin_login and "pw-support" in admin_login and "pw-auditor" not in admin_login
        assert "Test credentials" in lab_login and "pw-auditor" in lab_login and 'data-fill-lab="sunrise"' in lab_login and "pw-support" not in lab_login
        assert "pw-support" in recruiter and "pw-auditor" in recruiter and "pw-support" in demo and "pw-auditor" in demo
        for page in (admin_login, lab_login, recruiter, demo):
            assert "pw-admin" not in page and "pw-rad" not in page, "a write-capable account must never be published"
    finally:
        get_settings.cache_clear()


def test_the_stylesheet_is_responsive_and_themed() -> None:
    css = (STATIC / "app.css").read_text()
    for width in ("1080px", "900px", "760px"):
        assert f"@media (max-width: {width})" in css, f"no layout change at {width}"
    assert "prefers-color-scheme: dark" in css or '[data-theme="dark"]' in css, "no dark theme"
    assert ".public-menu-toggle:checked" in css, "the phone menu cannot open"
