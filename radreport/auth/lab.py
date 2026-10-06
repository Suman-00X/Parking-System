"""Signing lab users in with a password, and the tokens that carry their identity afterwards.

Order: find the signing secret (token_secret, require_token_secret) -> sign in (login) and get an
access token (issue_access_token) plus a refresh token (_issue_refresh) -> check an access token on
each request (verify_access_token) -> trade a refresh token for a new pair (refresh), or end the
sign-in (logout) -> set or change a password (set_password, change_password), which signs the
user out everywhere (revoke_all). sign_in_to_lab, refresh_in_lab and logout_in_lab wrap these
with their own sessions for the API and the browser sign-in alike.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

import jwt
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from radreport.admin.auth import MIN_PASSWORD_LENGTH, hash_password, verify_password
from radreport.core.config import get_settings
from radreport.core.logging import get_logger
from radreport.core.types import ActorType
from radreport.db.models.identity import AppUser, LabRefreshToken
from radreport.db.models.orchestration import AuditLog

log = get_logger(__name__)

ALGORITHM: Final[str] = "HS256"
ISSUER: Final[str] = "radreport"
#: Used only in local/test/development when no secret is configured, so a laptop needs no setup.
_DEV_SECRET: Final[str] = "radreport-development-only-secret-do-not-deploy"
_DEV_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"local", "test", "development"})
MIN_SECRET_LENGTH: Final[int] = 32
#: Browser sign-in keeps the same tokens in httponly cookies, out of reach of page scripts.
ACCESS_COOKIE: Final[str] = "radreport_lab_access"
REFRESH_COOKIE: Final[str] = "radreport_lab_refresh"
#: The refresh cookie is sent only to the browser sign-in routes, never with ordinary requests.
REFRESH_COOKIE_PATH: Final[str] = "/ui"


class SignInFailed(Exception):
    """Wrong credentials, an unknown or inactive account, or a lab that is closed."""


class TokenInvalid(Exception):
    """An access or refresh token that is malformed, forged, expired or revoked."""


@dataclass(frozen=True, slots=True)
class LabClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_in: int
    """Seconds."""

    refresh_token: str
    """Returned once; only its hash is stored."""


def token_secret() -> str:
    """The secret access tokens are signed with."""
    settings = get_settings()
    configured = settings.lab_auth.token_secret
    if configured:
        return configured
    if settings.environment in _DEV_ENVIRONMENTS:
        return _DEV_SECRET
    raise RuntimeError("RADREPORT_LAB_AUTH__TOKEN_SECRET is not set; lab sign-in cannot run outside development without it")


def require_token_secret() -> None:
    """Refuse to start outside development without a strong, deliberate secret."""
    settings = get_settings()
    if settings.environment in _DEV_ENVIRONMENTS:
        return
    configured = settings.lab_auth.token_secret
    if len(configured) < MIN_SECRET_LENGTH or configured == _DEV_SECRET:
        raise RuntimeError(f"RADREPORT_LAB_AUTH__TOKEN_SECRET must be set to at least {MIN_SECRET_LENGTH} random characters outside development")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _hash(raw: str) -> str:
    """SHA-256 is enough for a token with 256 bits of `secrets` entropy."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_access_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, roles: frozenset[str] | list[str], now: dt.datetime | None = None) -> tuple[str, int]:
    """A signed access token and its lifetime in seconds."""
    issued = now or _now()
    lifetime = get_settings().lab_auth.access_ttl_minutes * 60
    claims = {"iss": ISSUER, "typ": "access", "sub": str(user_id), "tid": str(tenant_id), "roles": sorted(roles), "iat": int(issued.timestamp()), "exp": int(issued.timestamp()) + lifetime, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, token_secret(), algorithm=ALGORITHM), lifetime


def verify_access_token(token: str) -> LabClaims:
    """Check signature, expiry, issuer and type; no database round trip."""
    try:
        claims = jwt.decode(token, token_secret(), algorithms=[ALGORITHM], issuer=ISSUER, options={"require": ["exp", "iat", "sub", "tid", "typ"]})
    except jwt.PyJWTError as exc:
        raise TokenInvalid(f"access token rejected: {type(exc).__name__}") from exc
    if claims.get("typ") != "access" or not isinstance(claims.get("roles"), list):
        raise TokenInvalid("not an access token")
    try:
        return LabClaims(user_id=uuid.UUID(claims["sub"]), tenant_id=uuid.UUID(claims["tid"]), roles=frozenset(str(r) for r in claims["roles"]))
    except (ValueError, TypeError) as exc:
        raise TokenInvalid("access token carries a malformed identity") from exc


def tenant_of_refresh_token(raw: str) -> uuid.UUID:
    """A refresh token is `<tenant id>.<secret>`, so the caller knows which lab to open it in."""
    tenant_part, _, secret_part = raw.partition(".")
    try:
        tenant_id = uuid.UUID(tenant_part)
    except ValueError as exc:
        raise TokenInvalid("malformed refresh token") from exc
    if len(secret_part) < 32:
        raise TokenInvalid("malformed refresh token")
    return tenant_id


def _issue_refresh(session: Session, user: AppUser, *, family_id: uuid.UUID, user_agent: str | None, ip_address: str | None) -> tuple[str, LabRefreshToken]:
    raw = f"{user.tenant_id}.{secrets.token_urlsafe(32)}"
    record = LabRefreshToken(id=uuid.uuid4(), tenant_id=user.tenant_id, app_user_id=user.id, token_hash=_hash(raw), family_id=family_id, expires_at=_now() + dt.timedelta(days=get_settings().lab_auth.refresh_ttl_days), user_agent=(user_agent or "")[:400] or None, ip_address=ip_address)
    session.add(record)
    return raw, record


def _pair(session: Session, user: AppUser, *, family_id: uuid.UUID, user_agent: str | None, ip_address: str | None) -> TokenPair:
    access, lifetime = issue_access_token(user_id=user.id, tenant_id=user.tenant_id, roles=list(user.roles or ()))
    refresh_raw, _record = _issue_refresh(session, user, family_id=family_id, user_agent=user_agent, ip_address=ip_address)
    session.flush()
    return TokenPair(access_token=access, access_expires_in=lifetime, refresh_token=refresh_raw)


def login(session: Session, *, tenant_id: uuid.UUID, email: str, password: str, user_agent: str | None = None, ip_address: str | None = None) -> TokenPair:
    """Check a lab user's password and start a sign-in; `session` must be bound to `tenant_id`."""
    user = session.execute(select(AppUser).where(AppUser.tenant_id == tenant_id, func.lower(AppUser.email) == email.strip().lower())).scalar_one_or_none()
    stored = user.password_hash if user else None
    ok = verify_password(password, stored)
    if stored is None:
        # Spend the same time as a real check, so response time does not reveal which accounts exist.
        verify_password(password, hash_password("x" * MIN_PASSWORD_LENGTH))
    if user is None or not ok or not user.is_active:
        log.warning("lab_login_failed", tenant_id=str(tenant_id), reason="unknown" if user is None else "inactive" if not user.is_active else "password", ip_address=ip_address)
        raise SignInFailed("invalid email or password")

    user.last_login_at = _now()
    pair = _pair(session, user, family_id=uuid.uuid4(), user_agent=user_agent, ip_address=ip_address)
    session.add(AuditLog(tenant_id=tenant_id, actor_id=user.id, actor_type=ActorType.USER, action="lab_user_logged_in", entity_type="app_user", entity_id=user.id, after={"ip_address": ip_address}))
    session.flush()
    log.info("lab_user_logged_in", tenant_id=str(tenant_id), user_id=str(user.id))
    return pair


def refresh(session: Session, *, raw: str, user_agent: str | None = None, ip_address: str | None = None) -> TokenPair:
    """Trade a refresh token for a new pair; a replayed old token revokes its whole sign-in."""
    record = session.execute(select(LabRefreshToken).where(LabRefreshToken.token_hash == _hash(raw))).scalar_one_or_none()
    if record is None:
        raise TokenInvalid("unknown refresh token")
    now = _now()
    if record.revoked_at is not None:
        # Someone holds a token that was already traded in: assume it was stolen and end the sign-in.
        revoked = _revoke_family(session, record.family_id, now)
        log.warning("lab_refresh_token_reused", tenant_id=str(record.tenant_id), user_id=str(record.app_user_id), family_revoked=revoked)
        raise TokenInvalid("refresh token already used; signed out everywhere for this sign-in")
    expires = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=dt.UTC)
    if expires <= now:
        raise TokenInvalid("refresh token expired")

    user = session.get(AppUser, record.app_user_id)
    if user is None or not user.is_active:
        _revoke_family(session, record.family_id, now)
        raise TokenInvalid("account is no longer active")

    record.revoked_at = now
    return _pair(session, user, family_id=record.family_id, user_agent=user_agent, ip_address=ip_address)


def _revoke_family(session: Session, family_id: uuid.UUID, now: dt.datetime) -> int:
    result = session.execute(update(LabRefreshToken).where(LabRefreshToken.family_id == family_id, LabRefreshToken.revoked_at.is_(None)).values(revoked_at=now))
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


def logout(session: Session, *, raw: str) -> bool:
    """End the sign-in this refresh token belongs to. Idempotent."""
    record = session.execute(select(LabRefreshToken).where(LabRefreshToken.token_hash == _hash(raw))).scalar_one_or_none()
    if record is None:
        return False
    _revoke_family(session, record.family_id, _now())
    return True


def revoke_all(session: Session, *, user_id: uuid.UUID) -> int:
    """End every sign-in of one user; their access tokens lapse within the access lifetime."""
    result = session.execute(update(LabRefreshToken).where(LabRefreshToken.app_user_id == user_id, LabRefreshToken.revoked_at.is_(None)).values(revoked_at=_now()))
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


def set_password(session: Session, *, user_id: uuid.UUID, password: str, actor_id: uuid.UUID | None, actor_is_platform: bool = False) -> AppUser:
    """Set a lab user's password (by a product admin, or by themselves) and sign them out everywhere."""
    user = session.get(AppUser, user_id)
    if user is None:
        raise ValueError(f"no lab user {user_id}")
    if not user.email:
        raise ValueError("this user has no email address to sign in with; add one through a roster import first")
    user.password_hash = hash_password(password)
    revoked = revoke_all(session, user_id=user.id)
    session.add(AuditLog(tenant_id=user.tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="lab_user_password_set", entity_type="app_user", entity_id=user.id, after={"by_platform_user": actor_is_platform, "sessions_revoked": revoked}))
    session.flush()
    log.info("lab_user_password_set", user_id=str(user.id), by_platform_user=actor_is_platform, sessions_revoked=revoked)
    return user


def change_password(session: Session, *, user_id: uuid.UUID, current: str, new: str) -> AppUser:
    """A lab user replaces their own password, proving the current one first."""
    user = session.get(AppUser, user_id)
    if user is None or not verify_password(current, user.password_hash):
        raise SignInFailed("current password is wrong")
    return set_password(session, user_id=user.id, password=new, actor_id=user.id)


def sign_in_to_lab(*, lab_slug: str, email: str, password: str, user_agent: str | None = None, ip_address: str | None = None) -> TokenPair:
    """Find the lab by slug and sign a user in; every failure raises the same SignInFailed."""
    from radreport.core.types import TenantStatus
    from radreport.db.models.tenancy import Tenant
    from radreport.db.session import system_session, tenant_session

    with system_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == lab_slug)).scalar_one_or_none()
        tenant_id = tenant.id if tenant and tenant.status != TenantStatus.OFFBOARDED else None
    if tenant_id is None:
        # Same failure as a wrong password, so the form cannot be used to list labs.
        verify_password(password, hash_password("x" * MIN_PASSWORD_LENGTH))
        raise SignInFailed("invalid lab, email or password")
    with tenant_session(tenant_id) as session:
        return login(session, tenant_id=tenant_id, email=email, password=password, user_agent=user_agent, ip_address=ip_address)


def refresh_in_lab(*, raw: str, user_agent: str | None = None, ip_address: str | None = None) -> TokenPair:
    """Trade a refresh token for a new pair in its own lab, committing any revocation before refusing."""
    from radreport.db.session import tenant_session

    tenant_id = tenant_of_refresh_token(raw)
    refused: TokenInvalid | None = None
    with tenant_session(tenant_id) as session:
        try:
            pair = refresh(session, raw=raw, user_agent=user_agent, ip_address=ip_address)
        except TokenInvalid as exc:
            # Caught inside the transaction on purpose: a refusal may have revoked a stolen token's
            # whole sign-in, and raising here would roll that revocation back.
            refused = exc
    if refused is not None:
        raise refused
    return pair


def logout_in_lab(*, raw: str) -> None:
    """End a sign-in by its refresh token; harmless if the token is malformed or unknown."""
    from radreport.db.session import tenant_session

    try:
        tenant_id = tenant_of_refresh_token(raw)
    except TokenInvalid:
        return
    with tenant_session(tenant_id) as session:
        logout(session, raw=raw)
