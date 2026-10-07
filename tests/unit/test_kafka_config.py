"""Kafka clients connect to a local broker in plain text, and to a hosted one over TLS with SASL credentials."""

from __future__ import annotations

import pytest

from radreport.core.config import get_settings
from radreport.events.bus import kafka_client_config


@pytest.fixture
def events_env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    for name in ("SECURITY_PROTOCOL", "SASL_MECHANISM", "USERNAME", "PASSWORD", "CA_LOCATION"):
        monkeypatch.delenv(f"RADREPORT_EVENTS__KAFKA_{name}", raising=False)

    def configure(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(f"RADREPORT_EVENTS__KAFKA_{key.upper()}", value)
        get_settings.cache_clear()

    yield configure
    get_settings.cache_clear()


def test_a_local_broker_is_plain_text(events_env) -> None:  # type: ignore[no-untyped-def]
    events_env(bootstrap="localhost:9092")
    assert kafka_client_config() == {"bootstrap.servers": "localhost:9092", "security.protocol": "PLAINTEXT"}


def test_a_hosted_broker_gets_tls_and_sasl(events_env) -> None:  # type: ignore[no-untyped-def]
    events_env(bootstrap="pkc-x.aws.confluent.cloud:9092", security_protocol="SASL_SSL", sasl_mechanism="PLAIN", username="KEY", password="SECRET", ca_location="/etc/ssl/ca.pem")
    assert kafka_client_config() == {"bootstrap.servers": "pkc-x.aws.confluent.cloud:9092", "security.protocol": "SASL_SSL", "sasl.mechanism": "PLAIN", "sasl.username": "KEY", "sasl.password": "SECRET", "ssl.ca.location": "/etc/ssl/ca.pem"}


def test_sasl_without_credentials_is_refused(events_env) -> None:  # type: ignore[no-untyped-def]
    events_env(security_protocol="SASL_SSL", sasl_mechanism="SCRAM-SHA-256")
    with pytest.raises(RuntimeError, match="KAFKA_USERNAME"):
        kafka_client_config()
