"""Tables operations staff change without a code release.

Defines: SystemConfig, one operational setting either platform-wide (tenant_id NULL) or overridden
for one lab; LabShard, a lab pinned to a database shard regardless of the hash ring.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.db.base import Base, TenantOptional, uuid_pk


class SystemConfig(Base, TenantOptional):
    """One setting's value. The key names an entry in `core.system_config.SETTINGS`."""

    __tablename__ = "system_config"
    # One platform-wide row per key, and one per (lab, key): a plain unique constraint treats NULL tenants as distinct.
    __table_args__ = (Index("uq_system_config_global_key", "key", unique=True, postgresql_where=text("tenant_id IS NULL")), Index("uq_system_config_tenant_key", "tenant_id", "key", unique=True, postgresql_where=text("tenant_id IS NOT NULL")))

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    """The platform user who set it; no foreign key, like `audit_log.actor_id`."""

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class LabShard(Base):
    """A lab held on one shard whatever the ring says. Lives in the directory database; keyed by lab_id, not tenant_id, because it is read before any lab is bound."""

    __tablename__ = "lab_shard"

    lab_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True)
    shard_name: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    pinned_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
