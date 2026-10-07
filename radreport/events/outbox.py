"""Writing a domain event: one outbox row, in the same transaction as the change it reports.

Order: name the events the product emits (Topic) -> write one (emit). Nothing is sent here; the
relay sends it after the transaction commits, so a rolled-back change never announces itself.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from radreport.db.models.events import OutboxEvent


class Topic(StrEnum):
    RECORDING_INGESTED = "recording.ingested"
    DRAFT_READY = "draft.ready"
    REPORT_SIGNED = "report.signed"
    AUTONOMY_REVOKED = "autonomy.revoked"


def emit(session: Session, topic: str, payload: dict[str, Any], *, tenant_id: uuid.UUID | None) -> OutboxEvent:
    """Record an event inside the caller's transaction. Payloads carry ids, never clinical text."""
    event = OutboxEvent(event_id=uuid.uuid4(), tenant_id=tenant_id, topic=str(topic), key=str(tenant_id) if tenant_id else "platform", payload={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items()})
    session.add(event)
    session.flush()
    return event
