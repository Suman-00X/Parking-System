"""The transactional outbox: events commit with their change, relay once, and apply once per consumer even across a crash."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import select, text

from radreport.db.models.events import ConsumedEvent, OutboxEvent
from radreport.db.session import system_session, tenant_session
from radreport.events import consumers
from radreport.events.bus import Event, KafkaEventBus, PostgresEventBus
from radreport.events.outbox import Topic, emit
from radreport.events.relay import Relay

pytestmark = pytest.mark.db

TOPIC = f"test.{uuid.uuid4().hex[:6]}"


@pytest.fixture
def applied():
    seen: list[uuid.UUID] = []
    name = f"test-consumer-{uuid.uuid4().hex[:6]}"
    consumers.consumer(name, TOPIC)(lambda session, event: seen.append(event.event_id))
    yield seen
    consumers.unregister(name)


@pytest.fixture(autouse=True)
def _drain_existing(migrated_db: str):
    """Earlier tests leave real events behind; send them first so each test sees only its own."""
    while Relay(bus=PostgresEventBus(migrated_db), url=migrated_db).run_once():
        pass
    yield


def _unsent(db: str, tenant_id: uuid.UUID) -> list[OutboxEvent]:
    with tenant_session(tenant_id, url=db) as session:
        return list(session.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id, OutboxEvent.topic == TOPIC, OutboxEvent.published_at.is_(None))).scalars().all())


def test_a_rolled_back_change_announces_nothing(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    with pytest.raises(RuntimeError), tenant_session(tenant_id, url=migrated_db) as session:
        emit(session, TOPIC, {"x": 1}, tenant_id=tenant_id)
        raise RuntimeError("the change failed")
    assert _unsent(migrated_db, tenant_id) == []


def test_the_relay_delivers_each_event_once(migrated_db: str, two_tenants, applied) -> None:
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        for n in range(3):
            emit(session, TOPIC, {"n": n}, tenant_id=tenant_id)
    relay = Relay(bus=PostgresEventBus(migrated_db), url=migrated_db)
    assert relay.run_once() == 3
    assert relay.run_once() == 0
    assert len(applied) == 3 and _unsent(migrated_db, tenant_id) == []


def test_a_crash_between_publish_and_mark_still_applies_once(migrated_db: str, two_tenants, applied) -> None:
    """The relay publishes, then dies before marking the rows sent: the next pass resends, the consumer skips the repeat."""
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        event = emit(session, TOPIC, {"case": "crash"}, tenant_id=tenant_id)
        event_id = event.event_id

    class CrashAfterPublish(PostgresEventBus):
        def publish(self, events: list[Event]) -> None:
            super().publish(events)
            raise ConnectionError("relay died before marking")

    assert Relay(bus=CrashAfterPublish(migrated_db), url=migrated_db).run_once() == 0
    [still_unsent] = _unsent(migrated_db, tenant_id)
    assert still_unsent.attempts == 1 and "relay died" in (still_unsent.last_error or "")
    assert applied == [event_id], "the consumer applied it on the first, failed pass"

    assert Relay(bus=PostgresEventBus(migrated_db), url=migrated_db).run_once() == 1
    assert applied == [event_id], "and not again on redelivery"
    with system_session(migrated_db) as session:
        assert session.execute(select(ConsumedEvent).where(ConsumedEvent.event_id == event_id)).scalars().all()


def test_two_relays_never_send_the_same_event(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        for n in range(20):
            emit(session, TOPIC, {"n": n}, tenant_id=tenant_id)
    sent: list[uuid.UUID] = []
    lock = threading.Lock()

    class SlowBus:
        name = "slow"

        def publish(self, events: list[Event]) -> None:
            time.sleep(0.05)
            with lock:
                sent.extend(e.event_id for e in events if e.topic == TOPIC)

    def drain() -> None:
        relay = Relay(bus=SlowBus(), url=migrated_db, batch_size=3)
        while relay.run_once():
            pass

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 20 and len(set(sent)) == 20


def test_the_kafka_bus_keys_by_lab_and_consumers_deduplicate(migrated_db: str, two_tenants, applied) -> None:
    tenant_id, _ = two_tenants

    class FakeProducer:
        def __init__(self) -> None:
            self.messages: list[tuple[str, bytes, bytes]] = []

        def produce(self, topic: str, *, key: bytes, value: bytes, on_delivery) -> None:  # type: ignore[no-untyped-def]
            self.messages.append((topic, key, value))
            on_delivery(None, None)

        def flush(self, _timeout: float) -> int:
            return 0

    producer = FakeProducer()
    with tenant_session(tenant_id, url=migrated_db) as session:
        emit(session, TOPIC, {"via": "kafka"}, tenant_id=tenant_id)
    assert Relay(bus=KafkaEventBus(producer=producer, topic_prefix="radreport."), url=migrated_db).run_once() >= 1
    ours = [m for m in producer.messages if m[0] == f"radreport.{TOPIC}"]
    assert len(ours) == 1 and ours[0][1] == str(tenant_id).encode()

    class Message:
        def __init__(self, value: bytes, offset: int) -> None:
            self._value, self._offset = value, offset

        def error(self) -> None:
            return None

        def value(self) -> bytes:
            return self._value

        def offset(self) -> int:
            return self._offset

    class FakeConsumer:
        def __init__(self, values: list[bytes]) -> None:
            self.queue = [Message(v, i) for i, v in enumerate(values)]
            self.committed = 0

        def poll(self, _timeout: float):  # type: ignore[no-untyped-def]
            return self.queue.pop(0) if self.queue else None

        def commit(self, *, message, asynchronous: bool) -> None:  # type: ignore[no-untyped-def]
            self.committed += 1

    name = next(c.name for c in consumers.registered() if TOPIC in c.topics)
    # The same message twice: a consumer that crashed after applying and before committing its offset sees it again.
    fake = FakeConsumer([ours[0][2], ours[0][2]])
    assert consumers.run_kafka_consumer(name, kafka_consumer=fake, url=migrated_db, max_messages=5) == 2
    assert len(applied) == 1 and fake.committed == 2


def test_a_kafka_outage_leaves_events_unsent(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants

    class DownProducer:
        def produce(self, *_a, **_k) -> None:  # type: ignore[no-untyped-def]
            pass

        def flush(self, _timeout: float) -> int:
            return 1

    with tenant_session(tenant_id, url=migrated_db) as session:
        emit(session, TOPIC, {"x": 1}, tenant_id=tenant_id)
    assert Relay(bus=KafkaEventBus(producer=DownProducer()), url=migrated_db).run_once() == 0
    assert len(_unsent(migrated_db, tenant_id)) == 1


def test_revoking_autonomy_emits_an_event(migrated_db: str, two_tenants) -> None:
    from radreport.autonomy import grant
    from radreport.db.models.knowledge import AutonomyClass

    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        session.add(AutonomyClass(tenant_id=tenant_id, code="NORMAL_CXR", display_name="Normal chest", baseline_cse_rate=0.02, required_n=100, cusum_threshold=4.0))
        session.flush()
        grant.revoke(session, tenant_id=tenant_id, class_code="NORMAL_CXR", reason="test")
        topics = session.execute(text("SELECT topic FROM outbox_event WHERE tenant_id = :t"), {"t": tenant_id}).scalars().all()
    assert Topic.AUTONOMY_REVOKED in topics
