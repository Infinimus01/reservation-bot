from __future__ import annotations

from pika.exceptions import ChannelClosedByBroker

import shared.rabbitmq as shared_rabbitmq
from shared.config import RabbitMQSettings
from shared.rabbitmq import _broker_error_message, _broker_topology_error_message


def build_rabbitmq_settings() -> RabbitMQSettings:
    return RabbitMQSettings(
        url="amqp://guest:guest@127.0.0.1:5672/%2F",
        booking_exchange="selenium_bot.booking",
        booking_routing_key="booking_jobs",
        booking_queue="booking_jobs",
        booking_retry_exchange="selenium_bot.booking.retry",
        booking_retry_routing_key="booking_jobs.retry",
        booking_retry_queue="booking_jobs.retry",
        booking_retry_delay_ms=0,
        booking_max_retries=3,
        results_exchange="selenium_bot.results",
        results_routing_key="job_results",
        results_queue="job_results",
        worker_prefetch_count=3,
    )


def test_broker_topology_error_message_explains_retry_queue_ttl_mismatch(monkeypatch) -> None:
    settings = build_rabbitmq_settings()
    exc = ChannelClosedByBroker(
        406,
        "PRECONDITION_FAILED - inequivalent arg 'x-message-ttl' for queue "
        "'booking_jobs.retry' in vhost '/': received '0' but current is '5000'",
    )
    monkeypatch.setattr(
        shared_rabbitmq,
        "resolve_local_rabbitmq_management_url",
        lambda: "http://127.0.0.1:32772",
    )

    message = _broker_topology_error_message(settings, exc)

    assert "RABBITMQ_BOOKING_RETRY_DELAY_MS=0" in message
    assert "booking_jobs.retry" in message
    assert "http://127.0.0.1:32772" in message
    assert "immutable" in message


def test_broker_error_message_suggests_auto_discovery() -> None:
    settings = build_rabbitmq_settings()

    message = _broker_error_message(settings)

    assert "RABBITMQ_URL=auto" in message
