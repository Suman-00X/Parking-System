"""Missing optional infrastructure is run without, listed on /health: Kafka falls back to the Postgres bus, and absent S3 refuses audio with a clear error instead of failing at a vendor call."""

from __future__ import annotations

import pytest

from radreport.adapters.storage import object_store as store_module
from radreport.adapters.storage.object_store import S3ObjectStore, StorageUnavailable, UnconfiguredObjectStore, object_store
from radreport.core import fallbacks
from radreport.core.config import StorageSettings, get_settings
from radreport.events import bus as bus_module
from radreport.events.bus import KafkaEventBus, PostgresEventBus, get_bus


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    fallbacks.clear()
    get_settings.cache_clear()
    yield monkeypatch
    fallbacks.clear()
    get_settings.cache_clear()


def test_a_fallback_is_listed_once_and_never_fails_health() -> None:
    fallbacks.note("events", "kafka unusable")
    fallbacks.note("events", "kafka unusable")
    assert fallbacks.active() == {"events": "kafka unusable"}
    assert fallbacks._health() == {"ok": True, "active": {"events": "kafka unusable"}}


def test_the_postgres_bus_is_used_as_configured(fresh: pytest.MonkeyPatch) -> None:
    fresh.setenv("RADREPORT_EVENTS__BUS", "postgres")
    assert isinstance(get_bus(), PostgresEventBus) and fallbacks.active() == {}


def test_an_unreachable_kafka_falls_back_to_postgres(fresh: pytest.MonkeyPatch) -> None:
    fresh.setenv("RADREPORT_EVENTS__BUS", "kafka")

    def unreachable(self: KafkaEventBus, timeout_seconds: float = 10.0) -> None:
        raise RuntimeError("no broker")

    fresh.setattr(KafkaEventBus, "__init__", lambda self, **_: setattr(self, "producer", None))
    fresh.setattr(KafkaEventBus, "reachable", unreachable)
    assert isinstance(get_bus(), PostgresEventBus)
    assert "no broker" in fallbacks.active()["events"]


def test_incomplete_sasl_credentials_fall_back_to_postgres(fresh: pytest.MonkeyPatch) -> None:
    fresh.setenv("RADREPORT_EVENTS__BUS", "kafka")
    fresh.setenv("RADREPORT_EVENTS__KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    fresh.delenv("RADREPORT_EVENTS__KAFKA_USERNAME", raising=False)
    assert isinstance(bus_module.get_bus(), PostgresEventBus) and "events" in fallbacks.active()


def test_a_reachable_kafka_is_used(fresh: pytest.MonkeyPatch) -> None:
    fresh.setenv("RADREPORT_EVENTS__BUS", "kafka")

    class Producer:
        def list_topics(self, timeout: float) -> dict[str, str]:
            return {}

    fresh.setattr(KafkaEventBus, "__init__", lambda self, **_: setattr(self, "producer", Producer()) or setattr(self, "prefix", "radreport."))
    assert isinstance(get_bus(), KafkaEventBus) and fallbacks.active() == {}


def test_no_s3_configuration_refuses_audio_but_not_the_app(fresh: pytest.MonkeyPatch) -> None:
    fresh.setattr(store_module, "_aws_credentials_found", lambda: False)
    store = object_store(StorageSettings())
    assert isinstance(store, UnconfiguredObjectStore) and not store.exists("clinical/x.flac")
    with pytest.raises(StorageUnavailable, match="RADREPORT_STORAGE__BUCKET"):
        store.put("clinical/x.flac", b"x", content_type="audio/flac")
    assert "storage" in fallbacks.active()


def test_s3_is_used_with_credentials_from_the_aws_chain(fresh: pytest.MonkeyPatch) -> None:
    fresh.setattr(store_module, "_aws_credentials_found", lambda: True)
    assert isinstance(object_store(StorageSettings(sse_kms_key_id="k")), S3ObjectStore) and fallbacks.active() == {}
