from __future__ import annotations

from datetime import datetime

from master.availability import resolve_trigger_slots
from master.queue_dispatcher import AvailabilityQueueDispatcher
from master.task_store import TaskStore
from shared.models import AvailabilityTriggerRequest, BookingTask


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish_booking_job(self, job, *, message_id=None) -> str:
        self.messages.append(job)
        return message_id or f"msg-{len(self.messages)}"


def build_task(task_id: str, *, date: str = "2026-10-10", time: str = "09:00") -> BookingTask:
    return BookingTask(
        task_id=task_id,
        firstName="Ada",
        lastName="Lovelace",
        email="ada@example.com",
        phone="1234567890",
        zip="97220",
        country="United States Of America",
        date=date,
        time=time,
        ticket_count=1,
        last_updated=datetime(2026, 1, 1, 0, 0, 0),
    )


def test_dispatch_matching_tasks_queues_only_available_capacity(tmp_path) -> None:
    store = TaskStore(tmp_path / "master_state.db", require_availability_trigger=True)
    store.initialize()
    store.upsert_tasks(
        [
            build_task("task-1"),
            build_task("task-2"),
            build_task("task-3"),
            build_task("task-4"),
            build_task("task-5"),
        ]
    )
    publisher = FakePublisher()
    dispatcher = AvailabilityQueueDispatcher(store, publisher)

    slots = resolve_trigger_slots(
        AvailabilityTriggerRequest(
            availabilities=[
                {
                    "date": "2026/10/10",
                    "time": "09:00",
                    "quantity": 3,
                }
            ]
        )
    )

    result = dispatcher.dispatch_matching_tasks(
        slots,
        source="test-availability",
        metadata={"batch": "unit"},
    )

    assert result["published_tasks"] == 3
    assert [message.task.task_id for message in publisher.messages] == [
        "task-1",
        "task-2",
        "task-3",
    ]
    assert [task.task_id for task in store.list_tasks("queued")] == [
        "task-1",
        "task-2",
        "task-3",
    ]
    assert [task.task_id for task in store.list_tasks("pending")] == [
        "task-4",
        "task-5",
    ]
