from __future__ import annotations

from typing import Any, Protocol

from master.task_store import TaskStore
from shared.models import AvailabilitySlot, BookingJobMessage, BookingTask


class TaskSource(Protocol):
    def sync_if_due(self, force: bool = False) -> dict[str, Any]:
        ...


class BookingPublisher(Protocol):
    def publish_booking_job(
        self,
        job: BookingJobMessage,
        *,
        message_id: str | None = None,
    ) -> str:
        ...


class AvailabilityQueueDispatcher:
    def __init__(
        self,
        task_store: TaskStore,
        publisher: BookingPublisher,
        task_source: TaskSource | None = None,
    ) -> None:
        self.task_store = task_store
        self.publisher = publisher
        self.task_source = task_source

    def refresh_task_source(self, force: bool = False) -> dict[str, Any]:
        if self.task_source is None:
            return {"enabled": False, "source": "none"}
        return self.task_source.sync_if_due(force=force)

    def dispatch_matching_tasks(
        self,
        slots: list[AvailabilitySlot],
        *,
        source: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        trigger_result = self.task_store.apply_availability_trigger(
            slots,
            source=source,
            metadata=metadata,
        )

        matched_task_ids = [str(task_id) for task_id in trigger_result["matched_task_ids"]]
        matched_tasks = self.task_store.get_tasks(matched_task_ids)
        publication_errors: dict[str, str] = {}
        published_task_ids: list[str] = []
        queued_tasks: list[BookingTask] = []

        for task in matched_tasks:
            if task.status != "pending":
                continue
            if not self.task_store.is_task_dispatchable(task):
                continue

            try:
                message_id = self.publisher.publish_booking_job(
                    BookingJobMessage(
                        task=task,
                        source=source,
                        metadata=dict(metadata or {}),
                    )
                )
            except Exception as exc:
                publication_errors[task.task_id] = str(exc)[:500]
                continue

            queued_task = self.task_store.mark_task_queued(
                task.task_id,
                broker_message_id=message_id,
                broker_source=source,
                metadata={
                    "availability_dispatch_enqueued": True,
                    "availability_dispatch_message_id": message_id,
                },
            )
            if queued_task is None:
                publication_errors[task.task_id] = "Published to RabbitMQ but task was missing locally"
                continue

            published_task_ids.append(task.task_id)
            queued_tasks.append(queued_task)

        return {
            **trigger_result,
            "published_task_ids": published_task_ids,
            "published_tasks": len(published_task_ids),
            "publication_errors": publication_errors,
            "queued_tasks": queued_tasks,
        }
