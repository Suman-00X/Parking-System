"""Keeps each lab's data separate: who is asking, which lab they are in, and which lifecycle moves are allowed.

Order: open a scope for a lab or for the system (tenant_scope, system_scope) -> read the caller
back out inside it (current_tenant_id, current_tenant_id_or_none, current_principal) ->
check a lab's status change before making it (allowed_transitions, assert_transition_allowed).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from radreport.core.errors import CrossTenantAccess, NoTenantContext
from radreport.core.types import TenantStatus

#: The Postgres session variable every RLS policy reads.
TENANT_GUC: Final[str] = "app.current_tenant_id"

#: Set alongside it so policies and triggers can tell a platform principal from
#: a lab principal without a second round trip.
PRINCIPAL_GUC: Final[str] = "app.current_principal_kind"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Two disjoint realms."""

    id: uuid.UUID
    kind: str  # "app_user" | "platform_user" | "system"
    tenant_id: uuid.UUID | None
    """None only for `platform_user` and `system` before an org is selected."""


_current_tenant: ContextVar[uuid.UUID | None] = ContextVar("current_tenant", default=None)
_current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def current_tenant_id() -> uuid.UUID:
    """The tenant this unit of work is scoped to."""
    tid = _current_tenant.get()
    if tid is None:
        raise NoTenantContext("no tenant in context; open a `tenant_scope(...)` before touching tenant-scoped tables")
    return tid


def current_tenant_id_or_none() -> uuid.UUID | None:
    """For the narrow, enumerated cross-tenant paths only ( and `CROSS_TENANT_VIEWS`)."""
    return _current_tenant.get()


def current_principal() -> Principal | None:
    return _current_principal.get()


class tenant_scope:  # noqa: N801 - reads as a context manager, not a class
    """Bind a tenant (and optionally a principal) for the enclosing block."""

    __slots__ = ("_tenant_id", "_principal", "_tok_t", "_tok_p", "_prev_t", "_prev_p")

    def __init__(self, tenant_id: uuid.UUID, principal: Principal | None = None) -> None:
        self._tenant_id = tenant_id
        self._principal = principal

    def __enter__(self) -> uuid.UUID:
        existing = _current_tenant.get()
        if existing is not None and existing != self._tenant_id:
            raise CrossTenantAccess(f"cannot nest tenant {self._tenant_id} inside tenant {existing}; selecting another org replaces scope, it does not widen it")
        self._prev_t = existing
        self._prev_p = _current_principal.get()
        self._tok_t = _current_tenant.set(self._tenant_id)
        self._tok_p = _current_principal.set(self._principal or self._prev_p)
        return self._tenant_id

    def __exit__(self, *exc: object) -> None:
        _restore(self._tok_t, self._prev_t, self._tok_p, self._prev_p)


class system_scope:  # noqa: N801
    """For genuinely tenantless work: migrations, the lab list, metering rollups, the canonical gold set."""

    __slots__ = ("_tok_t", "_tok_p", "_prev_t", "_prev_p")

    def __enter__(self) -> None:
        self._prev_t = _current_tenant.get()
        self._prev_p = _current_principal.get()
        self._tok_t = _current_tenant.set(None)
        self._tok_p = _current_principal.set(Principal(id=uuid.UUID(int=0), kind="system", tenant_id=None))

    def __exit__(self, *exc: object) -> None:
        _restore(self._tok_t, self._prev_t, self._tok_p, self._prev_p)


def _restore(tok_t: object, prev_t: uuid.UUID | None, tok_p: object, prev_p: Principal | None) -> None:
    """Unwind a scope, tolerating a token from another context."""
    try:
        _current_tenant.reset(tok_t)  # type: ignore[arg-type]
        _current_principal.reset(tok_p)  # type: ignore[arg-type]
    except ValueError:
        _current_tenant.set(prev_t)
        _current_principal.set(prev_p)


# --------------------------------------------------------------------------
# every table carries tenant_id, with a short, explicit exception list.

#: No tenant column at all.
UNTENANTED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "tenant",  # it *is* the tenant
        "platform_user",  # belongs to no tenant
        # A product admin's login session.
        "admin_session",
        # Request counts for the rate limits every worker must share; keyed by caller, not lab.
        "rate_limit_counter",
        "alembic_version",
    }
)

#: `tenant_id` present but NULLABLE. NULL means "global / canonical".
NULLABLE_TENANT_TABLES: Final[frozenset[str]] = frozenset(
    {
        # Otherwise the Claude Sonnet 5 row is duplicated per lab and a price
        # change touches N rows.
        "model_provider",
        "model_definition",
        # already decided this: nullable only for a global starting lexicon.
        "lexicon_set",
        "lexicon_term",
        "lexicon_surface_variant",
        # NOT NULL here destroys the pooled canonical gold set, and with it the argument for pooling.
        "eval_set",
        "eval_item",
        "eval_run",
        "eval_result",
        # Pooled by design; a cross-tenant snapshot is the whole point,
        # and an adaptation run over one inherits that.
        "training_corpus_snapshot",
        "model_adaptation_run",
        # Operational thresholds: NULL is the platform-wide value, a lab id overrides it for that lab.
        "system_config",
        # System actions have no tenant.
        "audit_log",
    }
)

#: The only cross-tenant reads the admin panel genuinely needs. Enumerated
#: in one place on purpose. Anything not here is tenant-scoped.
CROSS_TENANT_VIEWS: Final[frozenset[str]] = frozenset(
    {
        "tenant",  # the lab list
        "v_tenant_metering_rollup",  # aggregate spend per tenant
        "v_canonical_eval_set",  # the pooled gold set
    }
)


# --------------------------------------------------------------------------
# tenant lifecycle --------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {TenantStatus.PROVISIONING: frozenset({TenantStatus.ONBOARDING, TenantStatus.OFFBOARDED}), TenantStatus.ONBOARDING: frozenset({TenantStatus.PILOT, TenantStatus.OFFBOARDED}), TenantStatus.PILOT: frozenset({TenantStatus.LIVE, TenantStatus.SUSPENDED, TenantStatus.OFFBOARDED}), TenantStatus.LIVE: frozenset({TenantStatus.SUSPENDED, TenantStatus.OFFBOARDED}), TenantStatus.SUSPENDED: frozenset({TenantStatus.LIVE, TenantStatus.PILOT, TenantStatus.OFFBOARDED}), TenantStatus.OFFBOARDED: frozenset()}

#: The transition that is gated on a readiness report rather than on a click.
S7_GATED_TRANSITION: Final[tuple[str, str]] = (TenantStatus.ONBOARDING, TenantStatus.PILOT)


def allowed_transitions(current: str) -> frozenset[str]:
    """The statuses a lab in `current` may move to."""
    return _ALLOWED_TRANSITIONS.get(str(current), frozenset())


class TenantTransitionError(CrossTenantAccess):
    """Refused `tenant.status` change."""


def assert_transition_allowed(current: str, target: str, *, s7_readiness_passed: bool | None = None) -> None:
    """Guard `tenant.status` moves."""
    # `str()` rather than `!r`: these are StrEnum members, and repr would put
    # `<TenantStatus.LIVE: 'live'>` in a message an operator has to read.
    current, target = str(current), str(target)

    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None:
        raise TenantTransitionError(f"unknown tenant status {current!r}")
    if target not in allowed:
        readable = ", ".join(sorted(str(a) for a in allowed)) or "none (terminal)"
        raise TenantTransitionError(f"tenant status {current!r} cannot move to {target!r}; allowed: {readable}")
    if (current, target) == S7_GATED_TRANSITION and not s7_readiness_passed:
        raise TenantTransitionError("onboarding -> pilot is gated on readiness: every fail-severity check must pass first")
