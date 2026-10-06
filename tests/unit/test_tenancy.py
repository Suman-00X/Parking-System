"""Lab-separation rules that can be checked without a database."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.errors import CrossTenantAccess, NoTenantContext
from radreport.core.tenancy import CROSS_TENANT_VIEWS, Principal, TenantTransitionError, assert_transition_allowed, current_tenant_id, current_tenant_id_or_none, system_scope, tenant_scope
from radreport.core.types import TenantStatus


def test_reading_without_a_tenant_raises_rather_than_returning_none() -> None:
    """Returning None would let an unscoped query reach SQL."""
    with pytest.raises(NoTenantContext):
        current_tenant_id()


def test_scope_binds_and_releases() -> None:
    tenant = uuid.uuid4()
    with tenant_scope(tenant):
        assert current_tenant_id() == tenant
    assert current_tenant_id_or_none() is None


def test_nesting_a_different_tenant_is_refused() -> None:
    """The property: selecting another org **replaces** scope, never widens it."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with tenant_scope(a), pytest.raises(CrossTenantAccess):
        with tenant_scope(b):
            pass


def test_nesting_the_same_tenant_is_fine() -> None:
    tenant = uuid.uuid4()
    with tenant_scope(tenant), tenant_scope(tenant):
        assert current_tenant_id() == tenant


def test_system_scope_clears_the_tenant() -> None:
    tenant = uuid.uuid4()
    with tenant_scope(tenant), system_scope():
        assert current_tenant_id_or_none() is None


def test_platform_principal_carries_no_tenant() -> None:
    """A product admin belongs to no tenant, which is why they cannot be an `app_user` row (that column is NOT NULL)."""
    principal = Principal(id=uuid.uuid4(), kind="platform_user", tenant_id=None)
    assert principal.kind == "platform_user"


def test_cross_tenant_views_are_a_short_enumerated_list() -> None:
    """ "Enumerate them in one file. Anything not on that list is tenant-scoped, no exceptions." """
    assert CROSS_TENANT_VIEWS == {"tenant", "v_tenant_metering_rollup", "v_canonical_eval_set"}


# ------------------------------------------------------------- lifecycle ----
def test_happy_path_lifecycle() -> None:
    assert_transition_allowed(TenantStatus.PROVISIONING, TenantStatus.ONBOARDING)
    assert_transition_allowed(TenantStatus.ONBOARDING, TenantStatus.PILOT, s7_readiness_passed=True)
    assert_transition_allowed(TenantStatus.PILOT, TenantStatus.LIVE)


def test_onboarding_to_pilot_is_gated_on_s7() -> None:
    """The single highest-value thing to wire into registration."""
    with pytest.raises(TenantTransitionError, match="gated on readiness"):
        assert_transition_allowed(TenantStatus.ONBOARDING, TenantStatus.PILOT, s7_readiness_passed=False)


def test_unevaluated_readiness_fails_closed() -> None:
    """Forgetting to check must not read as passing."""
    with pytest.raises(TenantTransitionError):
        assert_transition_allowed(TenantStatus.ONBOARDING, TenantStatus.PILOT, s7_readiness_passed=None)


def test_pilot_cannot_be_skipped() -> None:
    with pytest.raises(TenantTransitionError):
        assert_transition_allowed(TenantStatus.ONBOARDING, TenantStatus.LIVE)


def test_offboarded_is_terminal() -> None:
    for target in TenantStatus.values():
        with pytest.raises(TenantTransitionError):
            assert_transition_allowed(TenantStatus.OFFBOARDED, target)


def test_suspended_can_return_to_service() -> None:
    """`suspended` routes reports to the manual fallback path rather than failing them, so it must be reversible."""
    assert_transition_allowed(TenantStatus.SUSPENDED, TenantStatus.LIVE)
