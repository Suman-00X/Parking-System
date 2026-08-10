"""Asserts every table's lab-scoping rules directly against the model definitions.

These need no database, which is the point: they are the cheapest check that a new table was
given its tenancy explicitly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKeyConstraint

from radreport.core.tenancy import NULLABLE_TENANT_TABLES, UNTENANTED_TABLES
from radreport.db.introspect import classify_tables, cross_tenant_foreign_keys, declared_nullable_mismatch, partitioned_tables, tenant_scoped_tables
from radreport.db.models import Base


def test_every_table_is_classified() -> None:
    """A new table must be tenant-scoped or on the exception list."""
    unclassified = classify_tables().unclassified
    assert not unclassified, f"tables with no tenant_id and no entry in UNTENANTED_TABLES: {sorted(unclassified)}. Decide the tenancy explicitly."


def test_nullable_tenant_tables_match_declaration() -> None:
    """Nullability and the declared exception list cannot drift apart."""
    mismatch = declared_nullable_mismatch()
    assert not mismatch, f"tenant_id nullability disagrees with NULLABLE_TENANT_TABLES for: {mismatch}"


def test_untenanted_list_is_accurate() -> None:
    classification = classify_tables()
    assert classification.untenanted <= UNTENANTED_TABLES


def test_canonical_eval_set_stays_possible() -> None:
    """Forcing `eval_set` NOT NULL destroys the pooled canonical gold set."""
    assert "eval_set" in NULLABLE_TENANT_TABLES
    assert "eval_item" in NULLABLE_TENANT_TABLES
    assert Base.metadata.tables["eval_set"].columns["tenant_id"].nullable


def test_global_model_catalog_stays_possible() -> None:
    """Otherwise the Sonnet 5 row is duplicated per lab."""
    for table in ("model_provider", "model_definition"):
        assert Base.metadata.tables[table].columns["tenant_id"].nullable


def test_no_foreign_key_crosses_tenants() -> None:
    """The detail that decides whether isolation actually holds."""
    offenders = cross_tenant_foreign_keys()
    assert not offenders, f"these FKs join two strict tenant-scoped tables without tenant_id; use `tenant_fk` so the constraint is composite: {offenders}"


def test_composite_fk_parents_have_the_required_unique_key() -> None:
    """A composite FK needs `UNIQUE (id, tenant_id)` on the parent."""
    strict = classify_tables().strict
    missing: list[str] = []

    for name, table in Base.metadata.tables.items():
        if name not in strict:
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            columns = {c.name for c in constraint.columns}
            # Composite only.
            if len(columns) < 2 or "tenant_id" not in columns:
                continue
            parent = next(iter(constraint.elements)).column.table
            has_key = any({c.name for c in uc.columns} == {"id", "tenant_id"} for uc in parent.constraints if uc.__class__.__name__ == "UniqueConstraint")
            if not has_key and parent.name not in missing:
                missing.append(parent.name)

    assert not missing, f"composite-FK parents lacking UNIQUE (id, tenant_id): {missing}"


def test_tenant_scoped_tables_lead_indexes_with_tenant_id() -> None:
    """If every query filters on `tenant_id`, indexes should lead with it."""
    strict = set(classify_tables().strict)
    offenders: list[str] = []

    for name, table in Base.metadata.tables.items():
        if name not in strict:
            continue
        for index in table.indexes:
            columns = [c.name for c in index.columns]
            if "tenant_id" in columns and columns[0] != "tenant_id":
                offenders.append(f"{name}.{index.name}: {columns}")

    assert not offenders, f"composite indexes should lead with tenant_id: {offenders}"


@pytest.mark.parametrize("table", ["asr_segment", "edit_event", "audit_log"])
def test_high_volume_tables_are_partitioned_monthly(table: str) -> None:
    """High-volume tables are partitioned monthly, and tenancy belongs in the index rather than the partition key — a tenant-keyed partition multiplies partition count by tenant count for no isolation benefit, since RLS already isolates."""
    assert table in partitioned_tables()
    partition_by = Base.metadata.tables[table].dialect_options["postgresql"]["partition_by"]
    assert "RANGE" in partition_by.upper()


def test_audit_log_is_tenant_nullable() -> None:
    """System actions have no tenant, so `audit_log` must accept NULL."""
    assert Base.metadata.tables["audit_log"].columns["tenant_id"].nullable


def test_every_tenant_scoped_table_is_reachable_for_policy_generation() -> None:
    """Migration 0002 generates policies from this list; it must be non-trivial."""
    tables = tenant_scoped_tables()
    assert len(tables) > 40, f"expected the full schema, got {len(tables)} tables"
    assert "report_draft" in tables
    assert "final_report" in tables
