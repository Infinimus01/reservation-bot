from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.config import RabbitMQSettings, WorkerSettings
from shared.iproyal_proxy import build_iproyal_sticky_proxy
from shared.models import BookingJobMessage, BookingTask
from worker.worker_main import (
    ProxyAllocator,
    RabbitMQWorker,
)


def build_worker_settings() -> WorkerSettings:
    return WorkerSettings(
        worker_id="worker-01",
        worker_name="worker-01",
        email_provider="burner",
        max_tasks=1,
        status_poll_interval_seconds=0.05,
        idle_shutdown_seconds=0,
        flaresolverr_startup_timeout_seconds=90,
        flaresolverr_startup_poll_seconds=0,
        flaresolverr_count=1,
        flaresolverr_host="127.0.0.1",
        flaresolverr_base_port=8191,
        flaresolverr_discovery_mode="env",
        flaresolverr_docker_label="app=flaresolverr",
        flaresolverr_docker_image="ghcr.io/flaresolverr/flaresolverr:latest",
        flaresolverr_container_prefix="flaresolverr-worker",
        autostart_flaresolverr=False,
        rabbitmq=RabbitMQSettings(
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
            worker_prefetch_count=1,
        ),
    )


def build_job_message(**task_overrides: object) -> bytes:
    task = BookingTask(
        task_id="task-1",
        firstName="Ada",
        lastName="Lovelace",
        email="ada@example.com",
        phone="1234567890",
        zip="97220",
        country="United States Of America",
        date="2026-03-26",
        time="13:00",
        upstream_proxy="proxy-1",
        **task_overrides,
    )
    return BookingJobMessage(task=task, source="availability-checker").model_dump_json().encode(
        "utf-8"
    )


class FakeChannel:
    def __init__(self) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []

    def basic_ack(self, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        self.nacks.append((delivery_tag, requeue))


class FakeResultPublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish_job_result(self, result, *, message_id=None) -> str:
        self.messages.append(result)
        return message_id or "result-1"


def test_proxy_allocator_reuses_single_rotating_proxy(monkeypatch) -> None:
    rotating_proxy = "geo.example.test:12321:user:pass"
    monkeypatch.delenv("IPROYAL_PROXY", raising=False)

    async def fake_get_validated_proxies(*, needed, all_proxies):
        assert needed == 1
        return list(all_proxies)

    monkeypatch.setattr(
        "worker.worker_main.load_proxies_from_file",
        lambda _path: [rotating_proxy],
    )
    monkeypatch.setattr(
        "worker.worker_main.get_validated_proxies",
        fake_get_validated_proxies,
    )

    allocator = ProxyAllocator(max_tasks=3)

    assert allocator.acquire() == rotating_proxy
    assert allocator.acquire() == rotating_proxy
    assert allocator.acquire() == rotating_proxy
    assert allocator.in_use == set()


def test_proxy_allocator_keeps_multi_proxy_entries_exclusive(monkeypatch) -> None:
    proxies = ["proxy-1", "proxy-2"]
    monkeypatch.delenv("IPROYAL_PROXY", raising=False)

    async def fake_get_validated_proxies(*, needed, all_proxies):
        return list(all_proxies)[:needed]

    monkeypatch.setattr(
        "worker.worker_main.load_proxies_from_file",
        lambda _path: list(proxies),
    )
    monkeypatch.setattr(
        "worker.worker_main.get_validated_proxies",
        fake_get_validated_proxies,
    )

    allocator = ProxyAllocator(max_tasks=2)

    assert allocator.acquire() == "proxy-1"
    assert allocator.acquire() == "proxy-2"
    with pytest.raises(RuntimeError, match="No free validated proxy"):
        allocator.acquire()


def test_build_iproyal_sticky_proxy_replaces_existing_session_tags() -> None:
    proxy = build_iproyal_sticky_proxy(
        "geo.iproyal.com:12321:user:pass_country-gb_session-ABCDEFGH_lifetime-1h",
        country="us",
        lifetime="24h",
    )

    assert proxy.startswith("geo.iproyal.com:12321:user:pass_country-us_session-")
    assert proxy.endswith("_lifetime-24h")
    assert "_country-gb" not in proxy
    assert "_session-ABCDEFGH" not in proxy
    assert re.search(r"_session-[A-Za-z0-9]{8}_lifetime-24h$", proxy)


def test_proxy_allocator_generates_and_warms_iproyal_proxy(monkeypatch) -> None:
    warmed: list[tuple[str, float]] = []

    async def fake_check_proxy(proxy, timeout=15.0):
        warmed.append((proxy, timeout))
        return True

    monkeypatch.setenv("IPROYAL_PROXY", "geo.iproyal.com:12321:user:pass")
    monkeypatch.setenv("IPROYAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("IPROYAL_PROXY_LIFETIME", "24h")
    monkeypatch.setenv("IPROYAL_PROXY_WARMUP_ATTEMPTS", "1")
    monkeypatch.setenv("IPROYAL_PROXY_WARMUP_TIMEOUT_SECONDS", "7")
    monkeypatch.setattr("worker.worker_main.check_proxy", fake_check_proxy)
    monkeypatch.setattr(
        "worker.worker_main.load_proxies_from_file",
        lambda _path: pytest.fail("IPRoyal mode should not load proxies.txt"),
    )

    allocator = ProxyAllocator(max_tasks=2)
    first_proxy = allocator.acquire()
    second_proxy = allocator.acquire()

    assert first_proxy != second_proxy
    assert "_country-us_session-" in first_proxy
    assert first_proxy.endswith("_lifetime-24h")
    assert "_country-us_session-" in second_proxy
    assert second_proxy.endswith("_lifetime-24h")
    assert warmed == [(first_proxy, 7.0), (second_proxy, 7.0)]


def test_worker_round_robins_flaresolverr_urls() -> None:
    worker = RabbitMQWorker(build_worker_settings())
    worker.flaresolverr_urls = [
        "http://127.0.0.1:8191/v1",
        "http://127.0.0.1:8192/v1",
        "http://127.0.0.1:8193/v1",
    ]

    assignments = [worker._next_flaresolverr_url() for _ in range(5)]

    assert assignments == [
        "http://127.0.0.1:8191/v1",
        "http://127.0.0.1:8192/v1",
        "http://127.0.0.1:8193/v1",
        "http://127.0.0.1:8191/v1",
        "http://127.0.0.1:8192/v1",
    ]


def test_worker_message_handler_runs_bot_with_message_fields(monkeypatch) -> None:
    worker = RabbitMQWorker(build_worker_settings())
    worker.result_publisher = FakeResultPublisher()
    worker.flaresolverr_urls = ["http://127.0.0.1:8191/v1"]
    channel = FakeChannel()
    method = SimpleNamespace(delivery_tag=11)
    properties = SimpleNamespace(headers={})

    async def fake_ensure_task_email(task, _provider):
        return task, {}

    async def emit_success(**kwargs):
        kwargs["status_callback"](
            {
                "instance_id": kwargs["instance_id"],
                "stage": "STEP 2/6: Calendar Page",
                "outcome": "success",
                "error": "",
            }
        )

    run_instance_mock = AsyncMock(side_effect=emit_success)
    monkeypatch.setattr("worker.worker_main.ensure_task_email", fake_ensure_task_email)
    monkeypatch.setattr("worker.worker_main.run_instance", run_instance_mock)

    worker.handle_message(channel, method, properties, build_job_message())

    assert channel.acks == [11]
    assert channel.nacks == []
    assert run_instance_mock.await_count == 1
    call = run_instance_mock.await_args
    user_details = call.kwargs["user_details"]
    assert user_details.firstName == "Ada"
    assert user_details.lastName == "Lovelace"
    assert user_details.date == "2026-03-26"
    assert user_details.time == "13:00"
    assert call.kwargs["flaresolverr_url"] == "http://127.0.0.1:8191/v1"
    assert call.kwargs["run_metadata"]["skip_slot_availability_check"] is True
    assert worker.result_publisher.messages[0].status == "completed"


def test_worker_message_handler_nacks_transient_failures(monkeypatch) -> None:
    worker = RabbitMQWorker(build_worker_settings())
    worker.result_publisher = FakeResultPublisher()
    worker.flaresolverr_urls = ["http://127.0.0.1:8191/v1"]
    channel = FakeChannel()
    method = SimpleNamespace(delivery_tag=12)
    properties = SimpleNamespace(headers={})

    async def fake_ensure_task_email(task, _provider):
        return task, {}

    run_instance_mock = AsyncMock(side_effect=TimeoutError("Timed out"))
    monkeypatch.setattr("worker.worker_main.ensure_task_email", fake_ensure_task_email)
    monkeypatch.setattr("worker.worker_main.run_instance", run_instance_mock)

    worker.handle_message(channel, method, properties, build_job_message())

    assert channel.acks == []
    assert channel.nacks == [(12, False)]
    assert worker.result_publisher.messages == []


def test_worker_failed_booking_result_nacks_until_retry_limit(monkeypatch) -> None:
    worker = RabbitMQWorker(build_worker_settings())
    worker.result_publisher = FakeResultPublisher()
    worker.flaresolverr_urls = ["http://127.0.0.1:8191/v1"]
    channel = FakeChannel()
    method = SimpleNamespace(delivery_tag=13)
    properties = SimpleNamespace(headers={})

    async def fake_ensure_task_email(task, _provider):
        return task, {}

    async def emit_calendar_failure(**kwargs):
        kwargs["status_callback"](
            {
                "instance_id": kwargs["instance_id"],
                "stage": "STEP 2/6: Calendar Page",
                "outcome": "error",
                "error": "TimeoutError: blind slot submit failed",
            }
        )

    run_instance_mock = AsyncMock(side_effect=emit_calendar_failure)
    monkeypatch.setattr("worker.worker_main.ensure_task_email", fake_ensure_task_email)
    monkeypatch.setattr("worker.worker_main.run_instance", run_instance_mock)

    worker.handle_message(channel, method, properties, build_job_message())

    assert channel.acks == []
    assert channel.nacks == [(13, False)]
    assert worker.result_publisher.messages == []


def test_worker_failed_booking_result_publishes_terminal_result_after_retry_limit(
    monkeypatch,
) -> None:
    worker = RabbitMQWorker(build_worker_settings())
    worker.result_publisher = FakeResultPublisher()
    worker.flaresolverr_urls = ["http://127.0.0.1:8191/v1"]
    channel = FakeChannel()
    method = SimpleNamespace(delivery_tag=14)
    properties = SimpleNamespace(
        headers={
            "x-death": [
                {
                    "queue": worker.settings.rabbitmq.booking_queue,
                    "count": worker.settings.rabbitmq.booking_max_retries,
                }
            ]
        }
    )

    async def fake_ensure_task_email(task, _provider):
        return task, {}

    async def emit_calendar_failure(**kwargs):
        kwargs["status_callback"](
            {
                "instance_id": kwargs["instance_id"],
                "stage": "STEP 2/6: Calendar Page",
                "outcome": "error",
                "error": "TimeoutError: blind slot submit failed",
            }
        )

    run_instance_mock = AsyncMock(side_effect=emit_calendar_failure)
    monkeypatch.setattr("worker.worker_main.ensure_task_email", fake_ensure_task_email)
    monkeypatch.setattr("worker.worker_main.run_instance", run_instance_mock)

    worker.handle_message(channel, method, properties, build_job_message())

    assert channel.acks == [14]
    assert channel.nacks == []
    assert len(worker.result_publisher.messages) == 1
    assert worker.result_publisher.messages[0].status == "failed"
    assert worker.result_publisher.messages[0].stage == "STEP 2/6: Calendar Page"
