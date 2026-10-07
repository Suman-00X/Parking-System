"""Table for remembered model responses, so a repeat of an identical request is answered without a call.

Defines: LLMResponseCache, one response per (lab, request fingerprint), with an expiry.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from radreport.db.base import Base, TenantScoped, tenant_table_args, uuid_pk


class LLMResponseCache(Base, TenantScoped):
    """A model's reply to one exact request in one lab. Never shared between labs: the request holds that lab's dictation."""

    __tablename__ = "llm_response_cache"
    __table_args__ = tenant_table_args(UniqueConstraint("tenant_id", "cache_key"), Index("ix_llm_response_cache_expiry", "expires_at"))

    id: Mapped[uuid.UUID] = uuid_pk()
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    """sha256 over the model and every request field that changes the answer."""

    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_hit_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
