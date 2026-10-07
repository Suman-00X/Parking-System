"""Where relayed events go: one EventBus interface, with a Postgres and a Kafka implementation chosen by config.

Order: an event as it travels (Event) -> the interface (EventBus.publish) -> deliver straight to the
in-process consumers, deduplicated in Postgres (PostgresEventBus) -> or produce to Kafka topics keyed
by lab, for consumers in other processes (KafkaEventBus) -> pick one from settings (get_bus).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    event_id: uuid.UUID
    tenant_id: uuid.UUID | None
    topic: str
    key: str
    payload: dict[str, Any]

    def to_json(self) -> bytes:
        return json.dumps({"event_id": str(self.event_id), "tenant_id": str(self.tenant_id) if self.tenant_id else None, "topic": self.topic, "payload": self.payload}).encode()

    @classmethod
    def from_json(cls, raw: bytes, *, offset: int = 0) -> Event:
        body = json.loads(raw)
        tenant = body.get("tenant_id")
        return cls(id=offset, event_id=uuid.UUID(body["event_id"]), tenant_id=uuid.UUID(tenant) if tenant else None, topic=body["topic"], key=tenant or "platform", payload=body.get("payload") or {})


class EventBus(Protocol):
    name: str

    def publish(self, events: list[Event]) -> None:
        """Hand events on; raise if any could not be, so the relay leaves them unsent and tries again."""
        ...


class PostgresEventBus:
    """No broker: the relay applies each event to every subscribed consumer itself, deduplicated in consumed_event."""

    name = "postgres"

    def __init__(self, url: str | None = None) -> None:
        self.url = url

    def publish(self, events: list[Event]) -> None:
        from radreport.events.consumers import deliver

        for event in events:
            deliver(event, url=self.url)


class KafkaEventBus:
    """Produces to `<prefix><topic>` keyed by lab id, so a lab's events keep their order within a partition."""

    name = "kafka"

    def __init__(self, *, bootstrap: str | None = None, topic_prefix: str | None = None, producer: Any | None = None) -> None:
        settings = get_settings().events
        self.prefix = topic_prefix if topic_prefix is not None else settings.topic_prefix
        if producer is None:
            try:
                from confluent_kafka import Producer  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - only without the optional dependency
                raise RuntimeError("the kafka bus needs confluent-kafka: pip install 'radreport[kafka]'") from exc
            producer = Producer({"bootstrap.servers": bootstrap or settings.kafka_bootstrap, "enable.idempotence": True, "acks": "all"})
        self.producer = producer

    def publish(self, events: list[Event]) -> None:
        failures: list[str] = []

        def on_delivery(err: Any, _msg: Any) -> None:
            if err is not None:
                failures.append(str(err))

        for event in events:
            self.producer.produce(self.prefix + event.topic, key=event.key.encode(), value=event.to_json(), on_delivery=on_delivery)
        remaining = self.producer.flush(30)
        if remaining or failures:
            raise RuntimeError(f"kafka did not acknowledge {remaining or len(failures)} event(s): {'; '.join(failures[:3])}")


def get_bus(url: str | None = None) -> EventBus:
    """The configured bus. The hosted demo runs the Postgres one; Kafka runs locally and in the crash test."""
    return KafkaEventBus() if get_settings().events.bus == "kafka" else PostgresEventBus(url)
