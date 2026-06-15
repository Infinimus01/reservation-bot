from __future__ import annotations

import asyncio

from shared.config import RabbitMQSettings, WorkerSettings
from worker.flaresolverr_pool import FlaresolverrPool


def build_worker_settings(**overrides: object) -> WorkerSettings:
    values = {
        "worker_id": "worker-01",
        "worker_name": "worker-01",
        "email_provider": "burner",
        "max_tasks": 1,
        "status_poll_interval_seconds": 1,
        "idle_shutdown_seconds": 0,
        "flaresolverr_startup_timeout_seconds": 90,
        "flaresolverr_startup_poll_seconds": 0,
        "flaresolverr_count": 1,
        "flaresolverr_host": "127.0.0.1",
        "flaresolverr_base_port": 8191,
        "flaresolverr_discovery_mode": "env",
        "flaresolverr_docker_label": "app=flaresolverr",
        "flaresolverr_docker_image": "ghcr.io/flaresolverr/flaresolverr:latest",
        "flaresolverr_container_prefix": "flaresolverr-worker",
        "autostart_flaresolverr": False,
        "rabbitmq": RabbitMQSettings(
            url="amqp://guest:guest@127.0.0.1:5672/%2F",
            booking_exchange="selenium_bot.booking",
            booking_routing_key="booking_jobs",
            booking_queue="booking_jobs",
            booking_retry_exchange="selenium_bot.booking.retry",
            booking_retry_routing_key="booking_jobs.retry",
            booking_retry_queue="booking_jobs.retry",
            booking_retry_delay_ms=5000,
            booking_max_retries=3,
            results_exchange="selenium_bot.results",
            results_routing_key="job_results",
            results_queue="job_results",
            worker_prefetch_count=3,
        ),
    }
    values.update(overrides)
    return WorkerSettings(**values)


def test_wait_for_healthy_urls_retries_until_available(monkeypatch) -> None:
    pool = FlaresolverrPool(build_worker_settings())
    responses = [
        [],
        [],
        ["http://127.0.0.1:8191/v1"],
    ]

    async def fake_discover_url_candidates() -> tuple[list[str], str]:
        return ["http://127.0.0.1:8191/v1"], "env"

    async def fake_healthy_urls(_candidate_urls: list[str]) -> list[str]:
        return responses.pop(0)

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(pool, "_discover_url_candidates", fake_discover_url_candidates)
    monkeypatch.setattr(pool, "_healthy_urls", fake_healthy_urls)
    monkeypatch.setattr("worker.flaresolverr_pool.asyncio.sleep", fake_sleep)

    urls = asyncio.run(pool.wait_for_healthy_urls(timeout_seconds=5, poll_seconds=0))

    assert urls == ["http://127.0.0.1:8191/v1"]


def test_wait_for_healthy_urls_waits_for_configured_pool(monkeypatch) -> None:
    pool = FlaresolverrPool(
        build_worker_settings(
            flaresolverr_count=3,
            flaresolverr_discovery_mode="ports",
        )
    )
    candidate_urls = [
        "http://127.0.0.1:8191/v1",
        "http://127.0.0.1:8192/v1",
        "http://127.0.0.1:8193/v1",
    ]
    poll_index = 0

    async def fake_discover_url_candidates() -> tuple[list[str], str]:
        return candidate_urls, "ports"

    async def fake_healthy_urls(_candidate_urls: list[str]) -> list[str]:
        if poll_index == 0:
            return candidate_urls[:1]
        return candidate_urls

    async def fake_sleep(_seconds: float) -> None:
        nonlocal poll_index
        poll_index += 1

    monkeypatch.setattr(pool, "_discover_url_candidates", fake_discover_url_candidates)
    monkeypatch.setattr(pool, "_healthy_urls", fake_healthy_urls)
    monkeypatch.setattr("worker.flaresolverr_pool.asyncio.sleep", fake_sleep)

    urls = asyncio.run(pool.wait_for_healthy_urls(timeout_seconds=5, poll_seconds=0))

    assert urls == candidate_urls
    assert poll_index == 1


def test_wait_for_healthy_urls_returns_partial_pool_after_timeout(monkeypatch) -> None:
    pool = FlaresolverrPool(
        build_worker_settings(
            flaresolverr_count=3,
            flaresolverr_discovery_mode="ports",
        )
    )
    candidate_urls = [
        "http://127.0.0.1:8191/v1",
        "http://127.0.0.1:8192/v1",
        "http://127.0.0.1:8193/v1",
    ]

    async def fake_discover_url_candidates() -> tuple[list[str], str]:
        return candidate_urls, "ports"

    async def fake_healthy_urls(_candidate_urls: list[str]) -> list[str]:
        return candidate_urls[:1]

    monkeypatch.setattr(pool, "_discover_url_candidates", fake_discover_url_candidates)
    monkeypatch.setattr(pool, "_healthy_urls", fake_healthy_urls)

    urls = asyncio.run(pool.wait_for_healthy_urls(timeout_seconds=0, poll_seconds=0))

    assert urls == ["http://127.0.0.1:8191/v1"]
