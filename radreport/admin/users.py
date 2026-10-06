"""Adding and retiring the product admins and support accounts who sign in to the admin panel.

Order: list who has access (list_platform_users) -> add an account (create_platform_user) ->
switch one off or back on (set_active) -> replace a password (reset_password), or your own
(change_own_password).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.admin.auth import hash_password, revoke_all_sessions, set_password, verify_password
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, PlatformRole
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.tenancy import PlatformUser

log = get_logger(__name__)


class UserChangeRefused(Exception):
    """A platform-user change that would break access, with a reason the panel can show."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _audit(session: Session, *, actor_id: uuid.UUID | None, action: str, user: PlatformUser, after: dict) -> None:
    session.add(AuditLog(tenant_id=None, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action=action, entity_type="platform_user", entity_id=user.id, after=after))


def list_platform_users(session: Session) -> list[PlatformUser]:
    """Every platform account, active ones first."""
    return list(session.execute(select(PlatformUser).order_by(PlatformUser.is_active.desc(), PlatformUser.role, PlatformUser.email)).scalars().all())


def create_platform_user(session: Session, *, email: str, display_name: str, role: str, password: str, actor_id: uuid.UUID | None = None) -> PlatformUser:
    """Add a product admin or support account, with its first password."""
    email = email.strip().lower()
    if role not in PlatformRole.values():
        raise UserChangeRefused("bad_role", f"role must be one of {', '.join(PlatformRole.values())}")
    if not email or "@" not in email:
        raise UserChangeRefused("bad_email", "an email address is required")
    if not display_name.strip():
        raise UserChangeRefused("bad_name", "a display name is required")
    if session.execute(select(PlatformUser).where(func.lower(PlatformUser.email) == email)).scalar_one_or_none() is not None:
        raise UserChangeRefused("duplicate", f"{email} already has an account")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise UserChangeRefused("weak_password", str(exc)) from exc

    user = PlatformUser(email=email, display_name=display_name.strip(), role=role, password_hash=password_hash)
    session.add(user)
    session.flush()
    _audit(session, actor_id=actor_id, action="platform_user_created", user=user, after={"email": email, "role": role})
    session.flush()
    log.info("platform_user_created", email=email, role=role)
    return user


def _active_product_admins(session: Session) -> int:
    return session.execute(select(func.count()).select_from(PlatformUser).where(PlatformUser.role == PlatformRole.PRODUCT_ADMIN, PlatformUser.is_active.is_(True))).scalar_one()


def set_active(session: Session, *, user_id: uuid.UUID, active: bool, actor_id: uuid.UUID) -> PlatformUser:
    """Deactivate or reactivate an account; deactivation also ends its live sessions."""
    user = session.get(PlatformUser, user_id)
    if user is None:
        raise UserChangeRefused("not_found", f"no platform user {user_id}")
    if user.is_active == active:
        return user
    if not active:
        if user.id == actor_id:
            raise UserChangeRefused("self", "you cannot deactivate your own account")
        if user.role == PlatformRole.PRODUCT_ADMIN and _active_product_admins(session) <= 1:
            raise UserChangeRefused("last_admin", "this is the last active product admin; add another before deactivating it")

    user.is_active = active
    revoked = 0 if active else revoke_all_sessions(session, platform_user_id=user.id)
    _audit(session, actor_id=actor_id, action="platform_user_reactivated" if active else "platform_user_deactivated", user=user, after={"email": user.email, "sessions_revoked": revoked})
    session.flush()
    log.info("platform_user_active_changed", email=user.email, active=active, sessions_revoked=revoked)
    return user


def reset_password(session: Session, *, user_id: uuid.UUID, password: str, actor_id: uuid.UUID) -> PlatformUser:
    """Replace an account's password and sign it out everywhere."""
    user = session.get(PlatformUser, user_id)
    if user is None:
        raise UserChangeRefused("not_found", f"no platform user {user_id}")
    try:
        return set_password(session, email=user.email, password=password, actor_id=actor_id)
    except ValueError as exc:
        raise UserChangeRefused("weak_password", str(exc)) from exc


def change_own_password(session: Session, *, user_id: uuid.UUID, current: str, new: str) -> PlatformUser:
    """An admin replaces their own password, proving the current one first; every session ends."""
    user = session.get(PlatformUser, user_id)
    if user is None or not verify_password(current, user.password_hash):
        raise UserChangeRefused("wrong_password", "your current password is wrong")
    if current == new:
        raise UserChangeRefused("same_password", "choose a password different from the current one")
    try:
        return set_password(session, email=user.email, password=new, actor_id=user.id)
    except ValueError as exc:
        raise UserChangeRefused("weak_password", str(exc)) from exc
