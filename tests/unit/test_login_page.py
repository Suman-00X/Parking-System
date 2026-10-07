"""The login page's test-credentials tab, shown only when demo accounts are configured."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from radreport.core.config import get_settings

DEMO = [{"label": "Recruiter demo", "email": "demo@radreport.local", "password": "try-radreport-demo"}]


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_no_demo_accounts_means_no_credentials_tab(fresh_settings: pytest.MonkeyPatch) -> None:
    # Explicitly empty: a developer's .env may list demo accounts.
    fresh_settings.setenv("RADREPORT_DEMO_ACCOUNTS", "[]")
    page = TestClient(create_app()).get("/admin/login").text
    assert "Test credentials" not in page
    assert 'action="/admin/login"' in page


def test_demo_accounts_appear_on_their_own_tab(fresh_settings: pytest.MonkeyPatch) -> None:
    fresh_settings.setenv("RADREPORT_DEMO_ACCOUNTS", json.dumps(DEMO))
    page = TestClient(create_app()).get("/admin/login").text
    assert "Test credentials" in page
    assert 'data-fill-email="demo@radreport.local"' in page and "try-radreport-demo" in page
    assert '<span class="badge brand">support</span>' in page


def test_demo_account_values_are_escaped(fresh_settings: pytest.MonkeyPatch) -> None:
    fresh_settings.setenv("RADREPORT_DEMO_ACCOUNTS", json.dumps([{**DEMO[0], "label": "<script>x</script>"}]))
    page = TestClient(create_app()).get("/admin/login").text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
