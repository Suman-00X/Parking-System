"""Moves committed outbox events onto the bus, then marks them sent.

Order: lock a batch of unsent events in commit order (Relay.run_once -> claim_outbox, FOR UPDATE
SKIP LOCKED, so two relays never send the same batch) -> publish them -> mark them sent in the
same transaction that held the lock (mark_outbox) -> on a bus failure, record the error and leave
them for the next pass. A crash after publishing and before marking resends the batch; consumers
deduplicate, so each still applies an event once.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import text

from radreport.core.config import get_settings
from radreport.core.logging import get_logger
from radreport.db.session import system_session
from radreport.events.bus import Event, EventBus, get_bus

log = get_logger(__name__)


class Relay:
    def __init__(self, *, bus: EventBus | None = None, url: str | None = None, batch_size: int | None = None) -> None:
        self.url = url
        self.bus = bus or get_bus(url)
        self.batch_size = batch_size or get_settings().events.relay_batch_size

    def run_once(self) -> int:
        """Relay one batch; returns how many events were published."""
        with system_session(self.url) as session:
            rows = session.execute(text("SELECT id, event_id, tenant_id, topic, key, payload FROM claim_outbox(:n)"), {"n": self.batch_size}).all()
            if not rows:
                return 0
            events = [Event(id=r[0], event_id=r[1] if isinstance(r[1], uuid.UUID) else uuid.UUID(str(r[1])), tenant_id=r[2], topic=r[3], key=r[4], payload=r[5] or {}) for r in rows]
            ids = [e.id for e in events]
            try:
                self.bus.publish(events)
            except Exception as exc:  # noqa: BLE001 - recorded on the rows; they stay unsent
                session.execute(text("SELECT mark_outbox(CAST(:ids AS bigint[]), :err)"), {"ids": ids, "err": f"{type(exc).__name__}: {exc}"[:1000]})
                log.warning("outbox_publish_failed", bus=self.bus.name, events=len(ids), error=type(exc).__name__)
                return 0
            session.execute(text("SELECT mark_outbox(CAST(:ids AS bigint[]), NULL)"), {"ids": ids})
        log.info("outbox_relayed", bus=self.bus.name, events=len(ids))
        return len(ids)

    def run_forever(self, *, idle_seconds: float = 1.0) -> None:  # pragma: no cover - a loop around run_once
        while True:
            if not self.run_once():
                time.sleep(idle_seconds)
