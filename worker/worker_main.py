from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import functools
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pika
from dotenv import load_dotenv

from booking_playwright_worker import (
    DataDomeBlockError,
    OrderLimitError,
    SlotFullError,
    run_instance_playwright,
)
from flare_bot import (
    PROXIES_FILE,
    check_proxy,
    get_validated_proxies,
    load_proxies_from_file,
)
from shared.config import WorkerSettings
from shared.iproyal_proxy import (
    IPRoyalProxySettings,
    acquire_warmed_iproyal_proxy,
    proxy_display,
)
from shared.models import BookingJobMessage, JobResultMessage
from shared.rabbitmq import RabbitMQPublisher, broker_retry_count, declare_topology
from util import UserDetails
from worker.email_resolver import ensure_task_email


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("worker.main")


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

def _is_past_date(date_str: str) -> bool:
    """True if the booking date is before today (job can never succeed)."""
    from datetime import date as _date, datetime as _dt
    s = (date_str or "").strip()
    if not s:
        return False
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return _dt.strptime(s, fmt).date() < _date.today()
        except ValueError:
            continue
    return False


def _is_non_retryable(stage: str, error: str) -> bool:
    combined = f"{stage}\n{error}".lower()
    return any(
        m in combined
        for m in (
            "slot capacity reached",
            "maximum amount of orders",
            "orderlimitreached",
            "order limit reached",
            "slot full",
            "no longer available",
        )
    )


def _is_cf_block(error: str) -> bool:
    lowered = error.lower()
    return any(
        m in lowered
        for m in (
            "datadome", "cloudflare", "cf challenge",
            "just a moment", "checking your browser",
            "verification required",
        )
    )


def _is_timeout(error: str) -> bool:
    lowered = error.lower()
    return any(
        m in lowered
        for m in (
            "timeout", "timed out", "connection reset",
            "connection aborted", "transport error",
            "503", "502", "429", "waiting room timeout",
        )
    )


# ---------------------------------------------------------------------------
# Proxy allocator (unchanged from original)
# ---------------------------------------------------------------------------

class ProxyAllocator:
    def __init__(self, max_tasks: int) -> None:
        self.max_tasks = max_tasks
        self.iproyal_settings = IPRoyalProxySettings.from_env()
        self.all_proxies = (
            []
            if self.iproyal_settings.enabled
            else load_proxies_from_file(PROXIES_FILE)
        )
        self.validated_proxies: list[str] = []
        self.in_use: set[str] = set()
        self._lock = threading.Lock()

    def _iproyal_enabled(self) -> bool:
        return self.iproyal_settings.enabled

    def _allow_reuse(self) -> bool:
        return len(self.all_proxies) == 1

    def _available_proxies(self) -> list[str]:
        if self._allow_reuse():
            return list(self.validated_proxies)
        return [p for p in self.validated_proxies if p not in self.in_use]

    def acquire(self) -> str:
        with self._lock:
            if self._iproyal_enabled():
                return asyncio.run(
                    acquire_warmed_iproyal_proxy(
                        self.iproyal_settings,
                        warmup=check_proxy,
                        logger=logger,
                        context="Worker proxy allocator",
                    )
                )

            available = self._available_proxies()
            if not available:
                if not self.all_proxies:
                    raise RuntimeError(f"No proxies available in {PROXIES_FILE}")

                target = min(
                    len(self.all_proxies),
                    max(1, len(self.validated_proxies) + 1, self.max_tasks),
                )
                self.validated_proxies = asyncio.run(
                    get_validated_proxies(needed=target, all_proxies=self.all_proxies)
                )
                available = self._available_proxies()

            if not available:
                raise RuntimeError("No free validated proxy available")

            proxy = available[0]
            if not self._allow_reuse():
                self.in_use.add(proxy)
            return proxy

    def release(self, proxy: str) -> None:
        with self._lock:
            self.in_use.discard(proxy)


# ---------------------------------------------------------------------------
# DeliveryDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliveryDecision:
    ack: bool
    requeue: bool = False


# ---------------------------------------------------------------------------
# run_booking_job — calls Playwright engine, returns JobResultMessage
# ---------------------------------------------------------------------------

def run_booking_job(
    *,
    job: BookingJobMessage,
    worker_id: str,
    email_provider: str,
    instance_id: int,
    upstream_proxy: str,
    delivery_attempt: int,
) -> JobResultMessage:
    # Ensure the task has an email (generate one if not set)
    task, email_metadata = asyncio.run(
        ensure_task_email(job.task, email_provider)
    )

    callbacks: list[dict[str, Any]] = []

    user_details = UserDetails(
        unique_id=task.task_id,
        date=task.date,
        firstName=task.firstName,
        lastName=task.lastName,
        email=task.email,
        phone=task.phone,
        zip=task.zip,
        country=task.country,
        time=task.time,
        ticket_count=task.ticket_count,
        job_time=task.job_time,
        status=task.status,
        proxy="",
        upstream_proxy=upstream_proxy,
    )

    asyncio.run(
        run_instance_playwright(
            user_details=user_details,
            instance_id=instance_id,
            status_callback=lambda payload: callbacks.append(dict(payload)),
            run_metadata={
                "worker_id": worker_id,
                "task_id": task.task_id,
                "try_number": delivery_attempt + 1,
            },
        )
    )

    terminal = next(
        (p for p in reversed(callbacks) if p.get("outcome") != "running"),
        None,
    )
    if terminal is None:
        raise RuntimeError("run_instance_playwright returned without a terminal outcome")

    outcome = str(terminal.get("outcome", "")).strip().lower()
    stage = str(terminal.get("stage", "")).strip()
    error = str(terminal.get("error", "")).strip()
    metadata = {
        **email_metadata,
        "delivery_attempt": delivery_attempt,
        "instance_id": instance_id,
    }
    booking_ref = str(terminal.get("booking_ref", "")).strip()
    if booking_ref:
        metadata["booking_ref"] = booking_ref

    if outcome == "success":
        return JobResultMessage(
            task_id=task.task_id,
            worker_id=worker_id,
            status="completed",
            stage=stage or "Completed",
            failure_reason="",
            flaresolverr_url="",
            upstream_proxy=upstream_proxy,
            email=task.email,
            metadata=metadata,
        )

    if outcome == "interrupted":
        raise TimeoutError(error or "Worker interrupted")

    if _is_cf_block(error):
        raise DataDomeBlockError(error or "CF/DataDome block detected")

    if _is_timeout(error):
        raise TimeoutError(error or "Booking job timed out")

    return JobResultMessage(
        task_id=task.task_id,
        worker_id=worker_id,
        status="failed",
        stage=stage or "Failed",
        failure_reason=error,
        flaresolverr_url="",
        upstream_proxy=upstream_proxy,
        email=task.email,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# RabbitMQWorker — consumes jobs, dispatches to run_booking_job
# ---------------------------------------------------------------------------

class RabbitMQWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.proxy_allocator = ProxyAllocator(settings.max_tasks)
        self.result_publisher = RabbitMQPublisher(settings.rabbitmq)
        self.executor = ThreadPoolExecutor(max_workers=settings.max_tasks)
        self._next_instance_id = 0
        self._instance_lock = threading.Lock()
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    def run(self) -> None:
        logger.info(
            "Worker %s starting — connecting to %s (prefetch=%d, max_tasks=%d)",
            self.settings.worker_id,
            self.settings.rabbitmq.booking_queue,
            min(self.settings.rabbitmq.worker_prefetch_count, self.settings.max_tasks),
            self.settings.max_tasks,
        )

        self._connection = pika.BlockingConnection(
            pika.URLParameters(self.settings.rabbitmq.url)
        )
        self._channel = self._connection.channel()
        declare_topology(self._channel, self.settings.rabbitmq)
        self._channel.basic_qos(
            prefetch_count=max(
                1,
                min(self.settings.rabbitmq.worker_prefetch_count, self.settings.max_tasks),
            )
        )
        self._channel.basic_consume(
            queue=self.settings.rabbitmq.booking_queue,
            on_message_callback=self._on_message,
            auto_ack=False,
        )

        logger.info("Worker %s ready — waiting for jobs…", self.settings.worker_id)
        try:
            self._channel.start_consuming()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=False)
            if self._connection and self._connection.is_open:
                self._connection.close()

    def _on_message(
        self,
        channel: Any,
        method: Any,
        properties: Any,
        body: bytes,
    ) -> None:
        if self._connection is None:
            raise RuntimeError("RabbitMQ connection not initialised")

        future = self.executor.submit(self._handle_delivery, properties, body)
        future.add_done_callback(
            lambda done: self._connection.add_callback_threadsafe(
                functools.partial(self._finalize_delivery, channel, method.delivery_tag, done)
            )
        )

    def _finalize_delivery(
        self,
        channel: Any,
        delivery_tag: int,
        future: Future[DeliveryDecision],
    ) -> None:
        try:
            decision = future.result()
        except Exception:
            logger.exception("Worker crashed on delivery %s", delivery_tag)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return

        if decision.ack:
            channel.basic_ack(delivery_tag=delivery_tag)
        else:
            channel.basic_nack(delivery_tag=delivery_tag, requeue=decision.requeue)

    def _handle_delivery(self, properties: Any, body: bytes) -> DeliveryDecision:
        job = BookingJobMessage.model_validate_json(body)
        delivery_attempt = broker_retry_count(
            getattr(properties, "headers", None),
            self.settings.rabbitmq.booking_queue,
        )
        logger.info(
            "Worker %s received task %s (attempt %d)",
            self.settings.worker_id,
            job.task.task_id,
            delivery_attempt,
        )

        # Drop past-date jobs immediately — they can never succeed.
        # (Catches stale messages already sitting in RabbitMQ that bypass the
        # master's sheet-sync filter.) Ack without retry, no proxy, no browser.
        if _is_past_date(job.task.date):
            logger.warning(
                "Task %s has past booking date %s — dropping without retry",
                job.task.task_id,
                job.task.date,
            )
            self.result_publisher.publish_job_result(
                self._build_failure(
                    job=job,
                    upstream_proxy="",
                    delivery_attempt=delivery_attempt,
                    failure_reason=f"Booking date {job.task.date} is in the past",
                    stage="Expired",
                )
            )
            return DeliveryDecision(ack=True)

        # Allocate proxy (prefer task-level proxy if already set)
        allocated_proxy = not bool(job.task.upstream_proxy)
        upstream_proxy = job.task.upstream_proxy or self.proxy_allocator.acquire()

        if upstream_proxy:
            logger.info(
                "Worker %s → task %s using proxy %s (%s)",
                self.settings.worker_id,
                job.task.task_id,
                proxy_display(upstream_proxy),
                "task-provided" if not allocated_proxy else "allocated",
            )

        try:
            result = run_booking_job(
                job=job,
                worker_id=self.settings.worker_id,
                email_provider=self.settings.email_provider,
                instance_id=self._next_instance(),
                upstream_proxy=upstream_proxy,
                delivery_attempt=delivery_attempt,
            )

        except (TimeoutError, DataDomeBlockError) as exc:
            # Transient — retry up to max_retries with a fresh proxy next time
            if delivery_attempt < self.settings.rabbitmq.booking_max_retries:
                logger.warning(
                    "Retrying task %s after transient failure (attempt %d/%d): %s",
                    job.task.task_id,
                    delivery_attempt + 1,
                    self.settings.rabbitmq.booking_max_retries + 1,
                    exc,
                )
                return DeliveryDecision(ack=False, requeue=False)

            failure = self._build_failure(
                job=job,
                upstream_proxy=upstream_proxy,
                delivery_attempt=delivery_attempt,
                failure_reason=str(exc),
                stage="Retry limit reached",
            )
            self.result_publisher.publish_job_result(failure)
            return DeliveryDecision(ack=True)

        except Exception as exc:
            logger.exception(
                "Worker %s crashed on task %s", self.settings.worker_id, job.task.task_id
            )
            failure = self._build_failure(
                job=job,
                upstream_proxy=upstream_proxy,
                delivery_attempt=delivery_attempt,
                failure_reason=str(exc),
                stage="Worker crash",
            )
            self.result_publisher.publish_job_result(failure)
            return DeliveryDecision(ack=True)

        finally:
            if allocated_proxy and upstream_proxy:
                self.proxy_allocator.release(upstream_proxy)

        # Booking returned a result — check if retryable failure
        if result.status != "completed":
            if _is_non_retryable(result.stage, result.failure_reason):
                logger.warning(
                    "Task %s non-retryable failure: %s | %s",
                    job.task.task_id,
                    result.stage,
                    result.failure_reason,
                )
                self.result_publisher.publish_job_result(result)
                return DeliveryDecision(ack=True)

            if delivery_attempt < self.settings.rabbitmq.booking_max_retries:
                logger.warning(
                    "Retrying task %s after booking failure (attempt %d/%d): %s | %s",
                    job.task.task_id,
                    delivery_attempt + 1,
                    self.settings.rabbitmq.booking_max_retries + 1,
                    result.stage,
                    result.failure_reason,
                )
                return DeliveryDecision(ack=False, requeue=False)

            logger.warning(
                "Task %s hit retry limit — publishing terminal failure",
                job.task.task_id,
            )

        self.result_publisher.publish_job_result(result)
        return DeliveryDecision(ack=True)

    def _build_failure(
        self,
        *,
        job: BookingJobMessage,
        upstream_proxy: str,
        delivery_attempt: int,
        failure_reason: str,
        stage: str,
    ) -> JobResultMessage:
        return JobResultMessage(
            task_id=job.task.task_id,
            worker_id=self.settings.worker_id,
            status="failed",
            stage=stage,
            failure_reason=failure_reason[:500],
            flaresolverr_url="",
            upstream_proxy=upstream_proxy,
            email=job.task.email,
            metadata={"delivery_attempt": delivery_attempt},
        )

    def _next_instance(self) -> int:
        with self._instance_lock:
            iid = self._next_instance_id
            self._next_instance_id += 1
            return iid


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()
    settings = WorkerSettings.from_env()
    RabbitMQWorker(settings).run()


if __name__ == "__main__":
    main()
