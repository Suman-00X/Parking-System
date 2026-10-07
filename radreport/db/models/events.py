"""Tables for domain events: the outbox written alongside each change, and what each consumer has applied.

Defines: OutboxEvent (one event, in commit order, until the relay publishes it) and ConsumedEvent
(one consumer's record of one event, which makes redelivery harmless).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, PrimaryKeyConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.db.base import Base, TenantOptional


class OutboxEvent(Base, TenantOptional):
    """A domain event, written in the same transaction as the change it describes."""

    __tablename__ = "outbox_event"
    __table_args__ = (Index("ix_outbox_event_unsent", "id", postgresql_where=text("published_at IS NULL")), Index("ix_outbox_event_tenant", "tenant_id", "created_at"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    """Commit-ordered, so a lab's events are relayed in the order they happened."""

    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True, server_default=text("gen_random_uuid()"))
    """What consumers deduplicate on."""

    topic: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    """The partition key: the lab's id, so one lab's events stay in order on Kafka."""

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsumedEvent(Base):
    """One consumer has applied one event. Inserted in the consumer's own transaction, so a redelivered event is skipped."""

    __tablename__ = "consumed_event"
    __table_args__ = (PrimaryKeyConstraint("consumer", "event_id"),)

    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    consumed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
