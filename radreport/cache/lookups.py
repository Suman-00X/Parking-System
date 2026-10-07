"""The lookups worth caching, returned as frozen snapshots rather than ORM rows so a copy can outlive its session.

Order: a lab's configuration (tenant_config) -> a lab user's roles (user_roles) -> drop a value
after changing it (forget_tenant, forget_user).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from radreport.cache import request
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


def tenant_config(session: Session, tenant_id: uuid.UUID) -> TenantConfig | None:
    """One lab's configuration, read once per request."""

    def load() -> TenantConfig | None:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        return TenantConfig(id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status, training_pooling_consent=tenant.training_pooling_consent, consent_withdrawn=tenant.consent_withdrawn_at is not None)

    return request.request_cached(key("tenant_config", tenant_id), load)


def user_roles(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserRoles | None:
    """A lab user's roles in their own lab, read once per request; None for someone not in that lab."""

    def load() -> UserRoles | None:
        user = session.get(AppUser, user_id)
        if user is None or user.tenant_id != tenant_id:
            return None
        return UserRoles(user_id=user.id, tenant_id=user.tenant_id, is_active=user.is_active, roles=tuple(user.roles or ()), display_name=user.display_name)

    return request.request_cached(key("user_roles", tenant_id, user_id), load)


def forget_tenant(tenant_id: uuid.UUID) -> None:
    """Call after changing a lab's row."""
    request.forget(key("tenant_config", tenant_id))


def forget_user(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Call after changing a lab user's roles or active flag."""
    request.forget(key("user_roles", tenant_id, user_id))
