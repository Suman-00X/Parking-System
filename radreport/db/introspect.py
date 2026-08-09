"""Works out which tables belong to a lab by reading the model definitions, so the rules are never hand-listed.

Order: classify every table (classify_tables, tenant_scoped_tables), then report the problems --
columns that should not be optional (declared_nullable_mismatch), keys that cross labs
(cross_tenant_foreign_keys, nullable_tenant_foreign_keys) and which tables are partitioned
(partitioned_tables).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ForeignKeyConstraint, Table

from radreport.core.tenancy import NULLABLE_TENANT_TABLES, UNTENANTED_TABLES
from radreport.db.models import Base


@dataclass(frozen=True, slots=True)
class TenancyClassification:
    """How every table in the schema is scoped."""

    strict: frozenset[str]
    """`tenant_id NOT NULL` + an RLS policy that requires a match."""

    nullable: frozenset[str]
    """`tenant_id NULL` allowed; NULL means global/canonical."""

    untenanted: frozenset[str]
    """No tenant column at all."""

    unclassified: frozenset[str]
    """Has no `tenant_id` but is not on the exception list. **A build failure.**"""


def classify_tables() -> TenancyClassification:
    strict: set[str] = set()
    nullable: set[str] = set()
    untenanted: set[str] = set()
    unclassified: set[str] = set()

    for name, table in Base.metadata.tables.items():
        col = table.columns.get("tenant_id")
        if col is None:
            (untenanted if name in UNTENANTED_TABLES else unclassified).add(name)
        elif col.nullable:
            nullable.add(name)
        else:
            strict.add(name)

    return TenancyClassification(strict=frozenset(strict), nullable=frozenset(nullable), untenanted=frozenset(untenanted), unclassified=frozenset(unclassified))


def tenant_scoped_tables() -> tuple[str, ...]:
    """Every table that needs an RLS policy, strict or nullable."""
    c = classify_tables()
    return tuple(sorted(c.strict | c.nullable))


def declared_nullable_mismatch() -> tuple[str, ...]:
    """Tables whose nullability disagrees with `NULLABLE_TENANT_TABLES`."""
    c = classify_tables()
    return tuple(sorted((c.nullable ^ NULLABLE_TENANT_TABLES) & (c.nullable | c.strict)))


def cross_tenant_foreign_keys() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """FKs between two **strict** tenant-scoped tables that omit `tenant_id`."""
    return _fk_edges(strict_only=True)


def nullable_tenant_foreign_keys() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The plain FKs exempted above, for review."""
    c = classify_tables()
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for name, table in Base.metadata.tables.items():
        if name not in (c.strict | c.nullable):
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            parent = _referred_table(constraint)
            if parent is None or parent.name == "tenant":
                continue
            if parent.name not in (c.strict | c.nullable):
                continue
            cols = tuple(col.name for col in constraint.columns)
            if "tenant_id" in cols:
                continue
            if name in c.nullable or parent.name in c.nullable:
                out.append((name, parent.name, cols))
    return tuple(sorted(out))


def _fk_edges(*, strict_only: bool) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    c = classify_tables()
    pool = c.strict if strict_only else (c.strict | c.nullable)
    offenders: list[tuple[str, str, tuple[str, ...]]] = []

    for name, table in Base.metadata.tables.items():
        if name not in pool:
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            parent = _referred_table(constraint)
            if parent is None or parent.name not in pool:
                continue
            # `tenant` itself is the root: a plain FK to it is correct.
            if parent.name == "tenant":
                continue
            cols = tuple(col.name for col in constraint.columns)
            if "tenant_id" not in cols:
                offenders.append((name, parent.name, cols))

    return tuple(sorted(offenders))


def _referred_table(constraint: ForeignKeyConstraint) -> Table | None:
    for element in constraint.elements:
        return element.column.table
    return None


def partitioned_tables() -> tuple[str, ...]:
    """Tables declared `PARTITION BY` (: monthly)."""
    return tuple(sorted(name for name, table in Base.metadata.tables.items() if table.dialect_options["postgresql"].get("partition_by")))
