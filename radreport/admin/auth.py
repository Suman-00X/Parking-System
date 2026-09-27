"""Signing product admins in and out, and storing their passwords safely.

Order: set a password (hash_password, set_password) -> sign in (login, verify_password) ->
check the session on later requests (authenticate) -> sign out (logout, revoke_all_sessions).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ActorType, PlatformRole
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.tenancy import AdminSession, PlatformUser

log = get_logger(__name__)

#: scrypt parameters: n=2^15, r=8 — roughly 100 ms and 32 MB per hash.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_BYTES = 16

SESSION_COOKIE = "radreport_admin"
#: A working day plus a margin. Short enough that a forgotten session on a
#: shared machine expires the same day; long enough not to interrupt work.
SESSION_TTL = dt.timedelta(hours=12)

#: Minimum password length.
MIN_PASSWORD_LENGTH = 12


class AuthenticationFailed(Exception):
    """Wrong credentials, unknown account, or an inactive one."""


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all base64."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"a password must be at least {MIN_PASSWORD_LENGTH} characters; length resists an offline attack better than composition rules")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN, maxmem=64 * 1024 * 1024)
    return "$".join(["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), base64.b64encode(salt).decode("ascii"), base64.b64encode(derived).decode("ascii")])


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check against a stored hash."""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(hash_b64)), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, base64.b64decode(hash_b64))


def _token_hash(token: str) -> str:
    """Hash a bearer token with SHA-256 (fast is safe: 256 bits of `secrets` entropy)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    """Returned **once**, to be set as a cookie. Only its hash is stored."""

    session_id: uuid.UUID
    platform_user_id: uuid.UUID
    display_name: str
    role: str
    expires_at: dt.datetime


def set_password(session: Session, *, email: str, password: str, actor_id: uuid.UUID | None = None) -> PlatformUser:
    """Set or replace a product admin's password."""
    user = session.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
    if user is None:
        raise ValueError(f"no platform_user with email {email!r}")

    user.password_hash = hash_password(password)
    revoked = revoke_all_sessions(session, platform_user_id=user.id)

    session.add(AuditLog(tenant_id=None, actor_id=actor_id or user.id, actor_type=ActorType.USER, action="admin_password_set", entity_type="platform_user", entity_id=user.id, after={"email": email, "sessions_revoked": revoked}))
    session.flush()
    log.info("admin_password_set", email=email, sessions_revoked=revoked)
    return user


def login(session: Session, *, email: str, password: str, user_agent: str | None = None, ip_address: str | None = None) -> LoginResult:
    """Authenticate and open a session."""
    user = session.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()

    stored = user.password_hash if user else None
    ok = verify_password(password, stored)
    if stored is None:
        # Burn equivalent time on a throwaway hash rather than returning early.
        verify_password(password, hash_password("x" * MIN_PASSWORD_LENGTH))

    if user is None or not ok or not user.is_active:
        log.warning("admin_login_failed", email=email, reason=("unknown" if user is None else "inactive" if not user.is_active else "password"), ip_address=ip_address)
        raise AuthenticationFailed("invalid email or password")

    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.UTC)
    record = AdminSession(id=uuid.uuid4(), platform_user_id=user.id, token_hash=_token_hash(token), expires_at=now + SESSION_TTL, user_agent=(user_agent or "")[:400] or None, ip_address=ip_address)
    session.add(record)
    user.last_login_at = now

    session.add(AuditLog(tenant_id=None, actor_id=user.id, actor_type=ActorType.USER, action="admin_logged_in", entity_type="platform_user", entity_id=user.id, after={"session_id": str(record.id), "ip_address": ip_address}))
    session.flush()

    log.info("admin_logged_in", email=email, session_id=str(record.id))
    return LoginResult(token=token, session_id=record.id, platform_user_id=user.id, display_name=user.display_name, role=user.role, expires_at=record.expires_at)


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    platform_user_id: uuid.UUID
    display_name: str
    role: str
    session_id: uuid.UUID

    @property
    def is_product_admin(self) -> bool:
        return self.role == PlatformRole.PRODUCT_ADMIN


def authenticate(session: Session, token: str | None) -> AuthenticatedAdmin | None:
    """Resolve a session cookie, or None."""
    if not token:
        return None

    record = session.execute(select(AdminSession).where(AdminSession.token_hash == _token_hash(token))).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return None

    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    if expires <= dt.datetime.now(dt.UTC):
        return None

    user = session.get(PlatformUser, record.platform_user_id)
    if user is None or not user.is_active:
        # Deactivating an admin must take effect on their live sessions, not
        # only at their next login.
        return None

    return AuthenticatedAdmin(platform_user_id=user.id, display_name=user.display_name, role=user.role, session_id=record.id)


def logout(session: Session, *, token: str | None) -> bool:
    """Revoke one session. Idempotent."""
    if not token:
        return False
    record = session.execute(select(AdminSession).where(AdminSession.token_hash == _token_hash(token))).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = dt.datetime.now(dt.UTC)
    session.flush()
    log.info("admin_logged_out", session_id=str(record.id))
    return True


def revoke_all_sessions(session: Session, *, platform_user_id: uuid.UUID) -> int:
    """Revoke every live session for one admin. Returns how many."""
    now = dt.datetime.now(dt.UTC)
    records = list(session.execute(select(AdminSession).where(AdminSession.platform_user_id == platform_user_id, AdminSession.revoked_at.is_(None))).scalars().all())
    for record in records:
        record.revoked_at = now
    session.flush()
    return len(records)
