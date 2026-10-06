"""The local-accounts seed, which writes clear-text passwords, must never run on a real deployment."""

from __future__ import annotations

import pytest

from radreport.core.config import get_settings
from radreport.devtools.local_accounts import LAB_PEOPLE, PLATFORM_ACCOUNTS, require_local


def test_it_refuses_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADREPORT_ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="developer machines only"):
            require_local()
    finally:
        get_settings.cache_clear()


def test_every_role_gets_an_account() -> None:
    from radreport.core.types import PlatformRole, UserRole

    assert {role for role, _email, _name in PLATFORM_ACCOUNTS} == set(PlatformRole.values())
    assert {role for *_rest, roles in LAB_PEOPLE for role in roles} == set(UserRole.values())


def test_the_credentials_sheet_is_never_committed() -> None:
    from pathlib import Path

    assert "local-credentials.md" in Path(".gitignore").read_text().splitlines()
