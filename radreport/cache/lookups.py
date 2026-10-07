"""The lookups worth caching, returned as frozen snapshots rather than ORM rows so a copy can outlive its session.

Order: a lab's configuration (tenant_config) -> a lab user's roles (user_roles) -> drop a value
after changing it (forget_tenant, forget_user). Both read through the request cache and the shared
cache (cache/shared.py), keyed by lab.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from radreport.cache import shared
from radreport.cache.keys import key
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import Tenant


@dataclass(frozen=True, slots=True)
class TenantConfig:
    """The parts of a lab's row that pages and checks read."""

    id: uuid.UUID
    name: str
    slug: str
    status: str
    training_pooling_consent: bool
    consent_withdrawn: bool


@dataclass(frozen=True, slots=True)
class UserRoles:
    """Whether a lab user may act, and in which roles."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    is_active: bool
    roles: tuple[str, ...]
    display_name: str = ""


def _encode_tenant(value: TenantConfig | None) -> dict | None:
    return None if value is None else {**asdict(value), "id": str(value.id)}


def _decode_tenant(raw: dict | None) -> TenantConfig | None:
    return None if raw is None else TenantConfig(**{**raw, "id": uuid.UUID(raw["id"])})


def _encode_user(value: UserRoles | None) -> dict | None:
    return None if value is None else {**asdict(value), "user_id": str(value.user_id), "tenant_id": str(value.tenant_id), "roles": list(value.roles)}


def _decode_user(raw: dict | None) -> UserRoles | None:
    return None if raw is None else UserRoles(**{**raw, "user_id": uuid.UUID(raw["user_id"]), "tenant_id": uuid.UUID(raw["tenant_id"]), "roles": tuple(raw["roles"])})


def tenant_config(session: Session, tenant_id: uuid.UUID) -> TenantConfig | None:
    """One lab's configuration, read once per request and shared between requests for a few minutes."""

    def load() -> TenantConfig | None:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        # The identity map holds rows weakly; keep this one alive so the page's own read of it is free.
        session.info.setdefault("pinned_rows", []).append(tenant)
        return TenantConfig(id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status, training_pooling_consent=tenant.training_pooling_consent, consent_withdrawn=tenant.consent_withdrawn_at is not None)

    return shared.cached(key("tenant_config", tenant_id), load, ttl_seconds=shared.ttl(shared=300, local=30), encode=_encode_tenant, decode=_decode_tenant)


def user_roles(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserRoles | None:
    """A lab user's roles in their own lab; None for someone not in that lab."""

    def load() -> UserRoles | None:
        user = session.get(AppUser, user_id)
        if user is None or user.tenant_id != tenant_id:
            return None
        return UserRoles(user_id=user.id, tenant_id=user.tenant_id, is_active=user.is_active, roles=tuple(user.roles or ()), display_name=user.display_name)

    return shared.cached(key("user_roles", tenant_id, user_id), load, ttl_seconds=shared.ttl(shared=120, local=15), encode=_encode_user, decode=_decode_user)


def forget_tenant(tenant_id: uuid.UUID, session: Session | None = None) -> None:
    """Call after changing a lab's row; pass the session so the shared copy goes once it commits."""
    target = key("tenant_config", tenant_id)
    shared.invalidate_after_commit(session, target) if session is not None else shared.invalidate(target)


def forget_user(tenant_id: uuid.UUID, user_id: uuid.UUID, session: Session | None = None) -> None:
    """Call after changing a lab user's roles or active flag."""
    target = key("user_roles", tenant_id, user_id)
    shared.invalidate_after_commit(session, target) if session is not None else shared.invalidate(target)
