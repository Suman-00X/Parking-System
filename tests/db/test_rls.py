"""Proves one lab's connection cannot read another lab's rows, and that the cross-lab keys stay consistent."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from radreport.db.introspect import classify_tables, tenant_scoped_tables
from radreport.db.session import get_engine

pytestmark = pytest.mark.db


def test_connection_is_not_privileged(app_db_url: str) -> None:
    """Guards every other test in this file."""
    with get_engine(app_db_url).connect() as conn:
        row = conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert not row.rolsuper, "tests are connecting as a superuser, which bypasses RLS unconditionally; run `python -m radreport.db.bootstrap` and point RADREPORT_TEST_APP_DATABASE_URL at the resulting login"
    assert not row.rolbypassrls, "test role has BYPASSRLS; RLS would not apply"


def test_every_tenant_scoped_table_has_rls_enabled_and_forced(migrated_db: str) -> None:
    """Coverage test."""
    expected = set(tenant_scoped_tables())
    with get_engine(migrated_db).connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       count(p.polname) AS policy_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_policy p ON p.polrelid = c.oid
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
                """
            )
        ).all()

    actual = {r.relname: r for r in rows}
    missing_rls = sorted(t for t in expected if not actual.get(t) or not actual[t].relrowsecurity)
    not_forced = sorted(t for t in expected if actual.get(t) and not actual[t].relforcerowsecurity)
    no_policy = sorted(t for t in expected if actual.get(t) and actual[t].policy_count == 0)

    assert not missing_rls, f"tables without ROW LEVEL SECURITY enabled: {missing_rls}"
    assert not not_forced, f"tables without FORCE ROW LEVEL SECURITY: {not_forced}"
    assert not no_policy, f"tables with RLS enabled but no policy: {no_policy}"


def test_no_rows_are_visible_without_a_tenant(migrated_db: str, two_tenants) -> None:
    """The policy fails closed when `app.current_tenant_id` is unset."""
    tenant_a, _ = two_tenants
    _seed_minimal(migrated_db, tenant_a)

    with get_engine(migrated_db).connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
        count = conn.execute(text("SELECT count(*) FROM patient")).scalar_one()
    assert count == 0


def test_tenant_a_cannot_see_tenant_b(migrated_db: str, two_tenants) -> None:
    """The leak test."""
    tenant_a, tenant_b = two_tenants
    _seed_minimal(migrated_db, tenant_a)
    _seed_minimal(migrated_db, tenant_b)

    strict = sorted(classify_tables().strict)
    leaks: list[str] = []

    with get_engine(migrated_db).connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_a)})
        for table in strict:
            visible = conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :other"), {"other": str(tenant_b)}).scalar_one()
            if visible:
                leaks.append(f"{table}: {visible} rows")

    assert not leaks, f"cross-tenant rows visible while scoped to A: {leaks}"


def test_writing_into_another_tenant_is_refused(migrated_db: str, two_tenants) -> None:
    """`WITH CHECK`, not just `USING`."""
    tenant_a, tenant_b = two_tenants

    with get_engine(migrated_db).connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_a)})
        with pytest.raises(Exception, match="row-level security|violates"):
            conn.execute(text("INSERT INTO patient (id, tenant_id, mrn, pseudonym, created_at, updated_at) VALUES (gen_random_uuid(), :other, 'X', 'Y', now(), now())"), {"other": str(tenant_b)})


def test_composite_foreign_keys_refuse_a_cross_tenant_parent(migrated_db: str, two_tenants) -> None:
    """Composite foreign keys refuse a parent row in another lab, proven at the database rather than asserted in metadata."""
    tenant_a, tenant_b = two_tenants
    patient_a = _seed_minimal(migrated_db, tenant_a)
    _seed_minimal(migrated_db, tenant_b)

    engine = get_engine(migrated_db)
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_b)})
        with pytest.raises(Exception, match="violates foreign key|row-level security"):
            conn.execute(text("INSERT INTO study (id, tenant_id, patient_id, metadata_source, priority, created_at, updated_at) VALUES (gen_random_uuid(), :b, :patient_a, 'upload', 'routine', now(), now())"), {"b": str(tenant_b), "patient_a": str(patient_a)})
            conn.commit()


def test_global_rows_are_visible_to_every_tenant(migrated_db: str, two_tenants) -> None:
    """The nullable-tenant policy: NULL means global."""
    tenant_a, tenant_b = two_tenants
    engine = get_engine(migrated_db)

    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
        conn.execute(text("INSERT INTO model_provider (id, tenant_id, name, kind, auth_method, is_active, created_at, updated_at) VALUES (gen_random_uuid(), NULL, :n, 'cloud_api', 'api_key', true, now(), now())"), {"n": f"global-{uuid.uuid4().hex[:8]}"})
        conn.commit()

    for tenant in (tenant_a, tenant_b):
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant)})
            visible = conn.execute(text("SELECT count(*) FROM model_provider WHERE tenant_id IS NULL")).scalar_one()
        assert visible > 0, "the global model catalog must be visible to every tenant"


def test_audit_log_denies_update_and_delete(migrated_db: str, app_db_url: str) -> None:
    """Append-only, enforced by grant rather than by convention."""
    with get_engine(app_db_url).connect() as conn:
        for statement in ("UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"):
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(text(statement))
            conn.rollback()


# ----------------------------------------------------------------- helpers --
def _seed_minimal(url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    """One patient per tenant — enough for the isolation assertions."""
    patient_id = uuid.uuid4()
    with get_engine(url).connect() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("INSERT INTO patient (id, tenant_id, mrn, pseudonym, created_at, updated_at) VALUES (:id, :t, :mrn, :pseudo, now(), now())"), {"id": str(patient_id), "t": str(tenant_id), "mrn": f"MRN-{patient_id.hex[:8]}", "pseudo": f"PT-{patient_id.hex[:8]}"})
        conn.commit()
    return patient_id
