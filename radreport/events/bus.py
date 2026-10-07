"""Where relayed events go: one EventBus interface, with a Postgres and a Kafka implementation chosen by config.

Order: an event as it travels (Event) -> the interface (EventBus.publish) -> deliver straight to the
in-process consumers, deduplicated in Postgres (PostgresEventBus) -> or produce to Kafka topics keyed
by lab, for consumers in other processes (KafkaEventBus) -> pick one from settings, falling back to
Postgres when Kafka cannot be reached (get_bus).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from radreport.core import fallbacks
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


def kafka_client_config(bootstrap: str | None = None) -> dict[str, str]:
    """Connection settings every Kafka client of the app shares: the broker, and for a hosted one TLS and SASL credentials."""
    settings = get_settings().events
    config = {"bootstrap.servers": bootstrap or settings.kafka_bootstrap, "security.protocol": settings.kafka_security_protocol}
    if settings.kafka_security_protocol.startswith("SASL"):
        if not (settings.kafka_sasl_mechanism and settings.kafka_username and settings.kafka_password):
            raise RuntimeError("a SASL broker needs RADREPORT_EVENTS__KAFKA_SASL_MECHANISM, RADREPORT_EVENTS__KAFKA_USERNAME and RADREPORT_EVENTS__KAFKA_PASSWORD")
        config |= {"sasl.mechanism": settings.kafka_sasl_mechanism, "sasl.username": settings.kafka_username, "sasl.password": settings.kafka_password}
    if settings.kafka_ca_location:
        config["ssl.ca.location"] = settings.kafka_ca_location
    return config


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
            producer = Producer({**kafka_client_config(bootstrap), "enable.idempotence": True, "acks": "all"})
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

    def reachable(self, timeout_seconds: float = 10.0) -> None:
        """Raise unless the broker answers a metadata request within the timeout."""
        self.producer.list_topics(timeout=timeout_seconds)


def get_bus(url: str | None = None) -> EventBus:
    """The configured bus; the Postgres one when Kafka is chosen but unusable (no package, incomplete credentials, no broker), since consumed_event keeps delivery exactly-once either way."""
    if get_settings().events.bus != "kafka":
        return PostgresEventBus(url)
    try:
        bus = KafkaEventBus()
        bus.reachable()
    except Exception as exc:  # noqa: BLE001 - any failure to reach Kafka means running without it
        fallbacks.note("events", f"kafka unusable ({type(exc).__name__}: {exc}"[:300] + "); events are applied in process through Postgres")
        return PostgresEventBus(url)
    return bus
