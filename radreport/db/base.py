"""The SQLAlchemy base class and the mixins that make a table carry its lab's id.

Defines: Base, the mixins (TimestampMixin, SoftDeleteMixin, TenantScoped, TenantOptional) and
the column helpers tables are built from (uuid_pk, tenant_fk, tenant_unique, enum_check,
array_enum_check, tenant_table_args).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Final

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, MetaData, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: Final[dict[str, str]] = {"ix": "ix_%(table_name)s_%(column_0_N_name)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_N_name)s", "pk": "pk_%(table_name)s"}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


# --------------------------------------------------------------- columns ---
def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


class TimestampMixin:
    """`created_at`/`updated_at` on every table."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SoftDeleteMixin:
    """Soft delete via `deleted_at` where retention matters."""

    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantScoped:
    """`tenant_id uuid NOT NULL` + the unique key composite FKs reference."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False)


class TenantOptional:
    """`tenant_id uuid NULL` — NULL means global/canonical."""

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True)


# ----------------------------------------------------------- constraints ---
def tenant_fk(local_column: str, target_table: str, *, ondelete: str | None = None, name: str | None = None) -> ForeignKeyConstraint:
    """A tenant-consistent foreign key."""
    return ForeignKeyConstraint([local_column, "tenant_id"], [f"{target_table}.id", f"{target_table}.tenant_id"], ondelete=ondelete, name=name or f"fk_{local_column}_tenant")


def tenant_unique() -> UniqueConstraint:
    """`UNIQUE (id, tenant_id)` — the parent side of a composite FK."""
    return UniqueConstraint("id", "tenant_id")


def enum_check(column: str, values: tuple[str, ...], *, name: str | None = None) -> CheckConstraint:
    """A CHECK built from `core.types`, so schema and code cannot drift."""
    rendered = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({rendered})", name=name or f"{column}_valid")


def array_enum_check(column: str, values: tuple[str, ...], *, name: str | None = None) -> CheckConstraint:
    """CHECK that every element of a text[] column is a known value."""
    rendered = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} <@ ARRAY[{rendered}]::text[]", name=name or f"{column}_valid")


def tenant_table_args(*extra: Any) -> tuple[Any, ...]:
    """Standard `__table_args__` for a tenant-scoped table."""
    return (tenant_unique(), *extra)
