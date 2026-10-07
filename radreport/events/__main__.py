"""Run the outbox relay: python -m radreport.events relay, or drain one Kafka consumer: python -m radreport.events consume analytics."""

from __future__ import annotations

import argparse

from radreport.core.config import get_settings
from radreport.core.logging import configure_logging, get_logger


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="radreport domain events")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("relay", help="publish committed outbox events to the configured bus")
    consume = sub.add_parser("consume", help="apply Kafka events for one consumer (bus=kafka only)")
    consume.add_argument("name")
    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "relay":
        from radreport.events.relay import Relay

        Relay().run_forever()
    else:
        from radreport.events.bus import KafkaEventBus, get_bus

        if not isinstance(get_bus(), KafkaEventBus):
            # The relay applies events in process when Kafka is off or unreachable; this consumer has nothing to read.
            get_logger(__name__).warning("kafka_consumer_idle", consumer=args.name, reason="kafka is not configured or not reachable")
            return

        from confluent_kafka import Consumer  # type: ignore[import-not-found]

        from radreport.events.bus import kafka_client_config
        from radreport.events.consumers import registered, run_kafka_consumer
        from radreport.events.outbox import Topic

        settings = get_settings().events
        target = next(c for c in registered() if c.name == args.name)
        kafka = Consumer({**kafka_client_config(), "group.id": f"radreport-{args.name}", "enable.auto.commit": False, "auto.offset.reset": "earliest"})
        kafka.subscribe([settings.topic_prefix + t.value for t in Topic if t.value in target.topics])
        run_kafka_consumer(args.name, kafka_consumer=kafka)


if __name__ == "__main__":
    main()
