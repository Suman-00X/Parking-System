"""Tables for the labs themselves, the product-admin accounts, and the training-data consent chain.

Defines: the lab (Tenant, TenantBranding), product-side accounts and their logins
(PlatformUser, AdminSession), and an append-only consent history (TrainingConsentEventLog).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, PrimaryKeyConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from radreport.core.types import PlatformRole, TenantStatus, TrainingConsentEvent
from radreport.db.base import Base, TimestampMixin, enum_check, uuid_pk


class Tenant(Base, TimestampMixin):
    """A lab. Anchors the `tenant_id` column on ~45 other tables."""

    __tablename__ = "tenant"
    __table_args__ = (
        enum_check("status", TenantStatus.values()),
        # `UNIQUE (id)` is implied by the PK, but composite FKs on children reference `(id, tenant_id)` on tenant-scoped parents — tenant itself is the root, so children point at plain `tenant.id`.
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    """Subdomain / URL key. Also the white-label host."""

    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=TenantStatus.PROVISIONING)

    # training-data pooling consent -----------------------------
    training_pooling_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Captured at registration from the signed contract ( step 1)."""

    training_consent_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Contract artefact reference."""

    patient_notice_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Which patient-notice wording this lab adopted.: you supply the model wording; labs will not draft it."""

    consent_effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Withdrawal stops *future* inclusion."""


class PlatformUser(Base, TimestampMixin):
    """Product admins. Belongs to no tenant, by design."""

    __tablename__ = "platform_user"
    __table_args__ = (enum_check("role", PlatformRole.values()),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """scrypt, as `scrypt$n$r$p$salt$hash`."""

    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class AdminSession(Base):
    """A logged-in product admin."""

    __tablename__ = "admin_session"
    __table_args__ = (ForeignKeyConstraint(["platform_user_id"], ["platform_user.id"], ondelete="CASCADE"), Index("ix_admin_session_token", "token_hash", unique=True), Index("ix_admin_session_user", "platform_user_id", "expires_at"))

    id: Mapped[uuid.UUID] = uuid_pk()
    platform_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Recorded for the audit trail, not for authorisation."""


class RateLimitCounter(Base):
    """Requests counted per (limit, caller, window), shared by every worker for the limits that need it."""

    __tablename__ = "rate_limit_counter"
    __table_args__ = (PrimaryKeyConstraint("limit_id", "who", "window_start"), Index("ix_rate_limit_counter_window", "window_start"))

    limit_id: Mapped[str] = mapped_column(Text, nullable=False)
    who: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class TrainingConsentEventLog(Base):
    """Append-only consent history, same discipline as `audit_log`."""

    __tablename__ = "training_consent_event"
    __table_args__ = (enum_check("event", TrainingConsentEvent.values()), Index("ix_training_consent_event_tenant_time", "tenant_id", "occurred_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("platform_user.id", ondelete="RESTRICT"), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class TenantBranding(Base, TimestampMixin):
    """White-label surface. Entirely new — not in the design doc."""

    __tablename__ = "tenant_branding"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False, unique=True)
    logo_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_colour: Mapped[str | None] = mapped_column(String(16), nullable=True)
    accent_colour: Mapped[str | None] = mapped_column(String(16), nullable=True)
    report_header_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_footer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    letterhead_block: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_sender_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Escalation paths are per-tenant already; the sender identity is not."""

    alert_email_templates: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
