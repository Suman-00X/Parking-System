"""Tables for background work: the job queue workers claim from.

Defines: Job, one unit of work with its lease, attempts and outcome.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import JobStatus
from radreport.db.base import Base, TenantOptional, enum_check, uuid_pk


class Job(Base, TenantOptional):
    """One unit of background work. Claimed with `claim_jobs()` (FOR UPDATE SKIP LOCKED), leased for a visibility timeout."""

    __tablename__ = "job"
    __table_args__ = (
        enum_check("status", JobStatus.values()),
        # What a worker scans when it claims: ready work by kind, best first.
        Index("ix_job_ready", "kind", "priority", "run_at", postgresql_where=text("status = 'queued'")),
        Index("ix_job_leased", "locked_until", postgresql_where=text("status = 'running'")),
        Index("ix_job_tenant", "tenant_id", "created_at"),
        # One live job per (lab, kind, dedupe key): a retried upload cannot queue the same pipeline run twice.
        Index("uq_job_dedupe", text("COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)"), "kind", "dedupe_key", unique=True, postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """The lease. A worker that dies stops renewing it, and the job becomes claimable again once it passes."""

    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
