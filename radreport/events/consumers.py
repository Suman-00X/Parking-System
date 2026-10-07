"""Consumers of domain events, each applying an event at most once however often it is delivered.

Order: register a consumer for topics (consumer) -> apply one event to every subscriber, each in
its own transaction bound to the event's lab, recording it in consumed_event in that same
transaction (deliver) -> the shipped consumers: analytics (one structured line per event) and
critical_alerts (a notification for each draft that carries an urgent finding). A Kafka consumer
process runs the same handlers through run_kafka_consumer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.db.models.events import ConsumedEvent
from radreport.db.session import system_session, tenant_session
from radreport.events.bus import Event
from radreport.events.outbox import Topic

log = get_logger(__name__)

Handler = Callable[[Session, Event], None]


@dataclass(frozen=True, slots=True)
class Consumer:
    name: str
    topics: frozenset[str]
    handle: Handler


_CONSUMERS: dict[str, Consumer] = {}


def consumer(name: str, *topics: str) -> Callable[[Handler], Handler]:
    """Subscribe `fn` to `topics` under a stable name; the name is what deduplication keys on."""

    def register(fn: Handler) -> Handler:
        _CONSUMERS[name] = Consumer(name=name, topics=frozenset(topics), handle=fn)
        return fn

    return register


def registered() -> list[Consumer]:
    return list(_CONSUMERS.values())


def unregister(name: str) -> None:
    _CONSUMERS.pop(name, None)


def apply_once(session: Session, sub: Consumer, event: Event) -> bool:
    """Apply one event for one consumer inside `session`; False if this consumer already has."""
    claimed = session.execute(insert(ConsumedEvent).values(consumer=sub.name, event_id=event.event_id).on_conflict_do_nothing().returning(ConsumedEvent.event_id)).scalar_one_or_none()
    if claimed is None:
        return False
    sub.handle(session, event)
    return True


def deliver(event: Event, *, url: str | None = None, only: str | None = None) -> int:
    """Apply `event` to each subscriber; a consumer's effects and its consumed_event row commit together. Returns how many applied it now."""
    applied = 0
    for sub in registered():
        if event.topic not in sub.topics or (only and sub.name != only):
            continue
        scope = tenant_session(event.tenant_id, url=url) if event.tenant_id else system_session(url)
        with scope as session:
            applied += int(apply_once(session, sub, event))
    return applied


def run_kafka_consumer(name: str, *, kafka_consumer: Any, url: str | None = None, max_messages: int | None = None) -> int:
    """Drain Kafka for one consumer: apply each message once, then commit its offset. Returns how many messages were read."""
    read = 0
    while max_messages is None or read < max_messages:
        message = kafka_consumer.poll(1.0)
        if message is None:
            if max_messages is not None:
                break
            continue
        if message.error():
            log.warning("kafka_consume_error", consumer=name, error=str(message.error()))
            continue
        deliver(Event.from_json(message.value(), offset=message.offset()), url=url, only=name)
        # After the database commit: a crash in between redelivers, and consumed_event absorbs it.
        kafka_consumer.commit(message=message, asynchronous=False)
        read += 1
    return read


# ------------------------------------------------------------- shipped -----
@consumer("analytics", *(t.value for t in Topic))
def _analytics(session: Session, event: Event) -> None:
    """One structured line per event, for the log pipeline's dashboards."""
    log.info("domain_event", topic=event.topic, event_id=str(event.event_id), tenant_id=str(event.tenant_id) if event.tenant_id else None, **{k: v for k, v in event.payload.items() if isinstance(v, str | int | float | bool)})


@consumer("critical_alerts", Topic.DRAFT_READY)
def _critical_alerts(session: Session, event: Event) -> None:
    """A draft with an urgent finding notifies whoever is on call; the alert row already exists, this is the push."""
    if event.payload.get("critical_alerts"):
        log.warning("critical_finding_notification", tenant_id=str(event.tenant_id), draft_id=event.payload.get("draft_id"), alerts=event.payload.get("critical_alerts"))
