"""Lab users' access tokens and the signing secret, without a database."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest

from radreport.auth import lab
from radreport.auth.lab import TokenInvalid, issue_access_token, require_token_secret, tenant_of_refresh_token, token_secret, verify_access_token
from radreport.core.config import get_settings

USER, TENANT = uuid.uuid4(), uuid.uuid4()


def test_a_fresh_token_round_trips() -> None:
    token, lifetime = issue_access_token(user_id=USER, tenant_id=TENANT, roles=["radiologist"])
    claims = verify_access_token(token)
    assert (claims.user_id, claims.tenant_id, claims.roles) == (USER, TENANT, frozenset({"radiologist"}))
    assert lifetime == 15 * 60


def test_an_expired_token_is_refused() -> None:
    token, _ = issue_access_token(user_id=USER, tenant_id=TENANT, roles=[], now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    with pytest.raises(TokenInvalid, match="Expired"):
        verify_access_token(token)


def test_a_token_signed_with_another_secret_is_refused() -> None:
    forged = jwt.encode({"iss": "radreport", "typ": "access", "sub": str(USER), "tid": str(TENANT), "roles": ["radiologist"], "iat": 0, "exp": 2**31}, "attacker-secret-attacker-secret-0000", algorithm="HS256")
    with pytest.raises(TokenInvalid):
        verify_access_token(forged)


def test_raised_roles_break_the_signature() -> None:
    token, _ = issue_access_token(user_id=USER, tenant_id=TENANT, roles=["auditor"])
    header, payload, signature = token.split(".")
    tampered_payload = jwt.utils.base64url_encode(jwt.utils.base64url_decode(payload).replace(b"auditor", b"radiologist")).decode()
    with pytest.raises(TokenInvalid):
        verify_access_token(f"{header}.{tampered_payload}.{signature}")


def test_the_none_algorithm_is_refused() -> None:
    unsigned = jwt.encode({"iss": "radreport", "typ": "access", "sub": str(USER), "tid": str(TENANT), "roles": [], "iat": 0, "exp": 2**31}, key=None, algorithm="none")
    with pytest.raises(TokenInvalid):
        verify_access_token(unsigned)


def test_a_token_of_another_type_is_refused() -> None:
    other = jwt.encode({"iss": "radreport", "typ": "refresh", "sub": str(USER), "tid": str(TENANT), "roles": [], "iat": 0, "exp": 2**31}, token_secret(), algorithm="HS256")
    with pytest.raises(TokenInvalid, match="not an access token"):
        verify_access_token(other)


def test_a_refresh_token_names_its_lab() -> None:
    assert tenant_of_refresh_token(f"{TENANT}.{'x' * 43}") == TENANT
    for bad in ("nonsense", f"{TENANT}.short", f"not-a-uuid.{'x' * 43}"):
        with pytest.raises(TokenInvalid):
            tenant_of_refresh_token(bad)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RADREPORT_ENVIRONMENT", "production")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_production_refuses_to_start_without_a_strong_secret(production: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="TOKEN_SECRET"):
        require_token_secret()
    production.setenv("RADREPORT_LAB_AUTH__TOKEN_SECRET", "short")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        require_token_secret()
    production.setenv("RADREPORT_LAB_AUTH__TOKEN_SECRET", lab._DEV_SECRET)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        require_token_secret()

    production.setenv("RADREPORT_LAB_AUTH__TOKEN_SECRET", "q" * 48)
    get_settings.cache_clear()
    require_token_secret()
    assert token_secret() == "q" * 48
