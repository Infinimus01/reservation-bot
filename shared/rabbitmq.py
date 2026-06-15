from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

import pika
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker

from shared.config import RabbitMQSettings, resolve_local_rabbitmq_management_url
from shared.models import BookingJobMessage, JobResultMessage


def _connection_parameters(settings: RabbitMQSettings) -> pika.URLParameters:
    return pika.URLParameters(settings.url)


@contextmanager
def open_channel(settings: RabbitMQSettings) -> Iterator[pika.adapters.blocking_connection.BlockingChannel]:
    connection = pika.BlockingConnection(_connection_parameters(settings))
    try:
        channel = connection.channel()
        try:
            declare_topology(channel, settings)
        except ChannelClosedByBroker as exc:
            raise RuntimeError(_broker_topology_error_message(settings, exc)) from exc
        yield channel
    finally:
        if connection.is_open:
            connection.close()


def ensure_broker_ready(settings: RabbitMQSettings) -> None:
    try:
        with open_channel(settings):
            return
    except AMQPConnectionError as exc:
        raise RuntimeError(_broker_error_message(settings)) from exc


def _broker_error_message(settings: RabbitMQSettings) -> str:
    parsed = urlparse(settings.url)
    host = parsed.hostname or "<unknown>"
    port = parsed.port or 5672
    message = (
        f"RabbitMQ connection failed for RABBITMQ_URL={settings.url!r} "
        f"(resolved target {host}:{port})."
    )
    if host in {"127.0.0.1", "localhost"} and port == 5672:
        message += (
            " If your broker is running in Docker with random published ports, "
            "set RABBITMQ_URL=auto so the current AMQP port is resolved at startup."
        )
    return message


def _broker_topology_error_message(
    settings: RabbitMQSettings,
    exc: ChannelClosedByBroker,
) -> str:
    detail = str(exc)
    if (
        "inequivalent arg 'x-message-ttl'" in detail
        and f"queue '{settings.booking_retry_queue}'" in detail
    ):
        return (
            f"RabbitMQ queue {settings.booking_retry_queue!r} already exists with a different "
            "x-message-ttl than the current "
            f"RABBITMQ_BOOKING_RETRY_DELAY_MS={settings.booking_retry_delay_ms}. "
            "RabbitMQ queue arguments are immutable once the queue is created. "
            f"Delete queue {settings.booking_retry_queue!r}{_management_ui_hint(settings)} and "
            "restart the app, or temporarily revert RABBITMQ_BOOKING_RETRY_DELAY_MS to the old value. "
            "Deleting the queue will discard any pending retry messages waiting there. "
            f"Broker said: {detail}"
        )
    return f"RabbitMQ rejected topology declaration: {detail}"


def _management_ui_hint(settings: RabbitMQSettings) -> str:
    parsed = urlparse(settings.url)
    host = parsed.hostname or ""
    if host in {"127.0.0.1", "localhost"}:
        resolved_management_url = resolve_local_rabbitmq_management_url()
        if resolved_management_url:
            return f" in the RabbitMQ management UI at {resolved_management_url}"
        return " in the RabbitMQ management UI"
    return ""


def declare_topology(
    channel: pika.adapters.blocking_connection.BlockingChannel,
    settings: RabbitMQSettings,
) -> None:
    channel.exchange_declare(
        exchange=settings.booking_exchange,
        exchange_type="direct",
        durable=True,
    )
    channel.exchange_declare(
        exchange=settings.booking_retry_exchange,
        exchange_type="direct",
        durable=True,
    )
    channel.exchange_declare(
        exchange=settings.results_exchange,
        exchange_type="direct",
        durable=True,
    )

    channel.queue_declare(
        queue=settings.booking_queue,
        durable=True,
        arguments={
            "x-dead-letter-exchange": settings.booking_retry_exchange,
            "x-dead-letter-routing-key": settings.booking_retry_routing_key,
        },
    )
    channel.queue_bind(
        queue=settings.booking_queue,
        exchange=settings.booking_exchange,
        routing_key=settings.booking_routing_key,
    )

    channel.queue_declare(
        queue=settings.booking_retry_queue,
        durable=True,
        arguments={
            "x-message-ttl": settings.booking_retry_delay_ms,
            "x-dead-letter-exchange": settings.booking_exchange,
            "x-dead-letter-routing-key": settings.booking_routing_key,
        },
    )
    channel.queue_bind(
        queue=settings.booking_retry_queue,
        exchange=settings.booking_retry_exchange,
        routing_key=settings.booking_retry_routing_key,
    )

    channel.queue_declare(queue=settings.results_queue, durable=True)
    channel.queue_bind(
        queue=settings.results_queue,
        exchange=settings.results_exchange,
        routing_key=settings.results_routing_key,
    )


class RabbitMQPublisher:
    def __init__(self, settings: RabbitMQSettings) -> None:
        self.settings = settings

    def publish_booking_job(
        self,
        job: BookingJobMessage,
        *,
        message_id: str | None = None,
    ) -> str:
        return self._publish(
            exchange=self.settings.booking_exchange,
            routing_key=self.settings.booking_routing_key,
            payload=job.model_dump(mode="json"),
            message_id=message_id,
        )

    def publish_job_result(
        self,
        result: JobResultMessage,
        *,
        message_id: str | None = None,
    ) -> str:
        return self._publish(
            exchange=self.settings.results_exchange,
            routing_key=self.settings.results_routing_key,
            payload=result.model_dump(mode="json"),
            message_id=message_id,
        )

    def _publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        payload: dict[str, Any],
        message_id: str | None = None,
    ) -> str:
        resolved_message_id = message_id or str(uuid4())
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        with open_channel(self.settings) as channel:
            channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    message_id=resolved_message_id,
                ),
                mandatory=False,
            )
        return resolved_message_id


def broker_retry_count(headers: Any, source_queue: str) -> int:
    if not isinstance(headers, dict):
        return 0

    x_death = headers.get("x-death")
    if not isinstance(x_death, list):
        return 0

    for entry in x_death:
        if not isinstance(entry, dict):
            continue
        if entry.get("queue") != source_queue:
            continue
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0
    return 0
