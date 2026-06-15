from __future__ import annotations

from datetime import datetime

import pytest

from master.availability import resolve_trigger_slots
from master.task_store import TaskStore
from shared.models import AvailabilityTriggerRequest, BookingTask


def build_task_store(tmp_path, *, require_availability_trigger: bool = False) -> TaskStore:
    store = TaskStore(
        tmp_path / "master_state.db",
        require_availability_trigger=require_availability_trigger,
    )
    store.initialize()
    return store


def build_task(task_id: str, *, date: str, time: str) -> BookingTask:
    return BookingTask(
        task_id=task_id,
        firstName="Ada",
        lastName="Lovelace",
        email="",
        phone="1234567890",
        zip="97220",
        country="United States Of America",
        date=date,
        time=time,
        ticket_count=1,
        last_updated=datetime(2026, 1, 1, 0, 0, 0),
    )


def test_resolve_trigger_slots_normalizes_payload_shape() -> None:
    request = AvailabilityTriggerRequest(
        source="availability-api",
        availabilities=[
            {
                "date": "2026/03/26",
                "time": "15:00",
                "quantity": 4,
            }
        ],
    )

    slots = resolve_trigger_slots(request)

    assert [slot.model_dump() for slot in slots] == [
        {"date": "2026-03-26", "time": "15:00", "quantity": 4}
    ]


def test_resolve_trigger_slots_rejects_non_contract_date_format() -> None:
    request = AvailabilityTriggerRequest(
        source="availability-api",
        availabilities=[
            {
                "date": "26 March 2026",
                "time": "15:00",
                "quantity": 4,
            }
        ],
    )

    with pytest.raises(ValueError, match="YYYY/MM/DD"):
        resolve_trigger_slots(request)


def test_claim_pending_tasks_requires_availability_trigger_when_enabled(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.upsert_tasks([build_task("task-1", date="2026-03-26", time="15:00")])

    claimed = store.claim_pending_tasks("worker-1", 10)

    assert claimed == []


def test_apply_availability_trigger_dispatches_only_matching_tasks(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.upsert_tasks(
        [
            build_task("match-1", date="2026-03-26", time="15:00").model_copy(
                update={"ticket_count": 2}
            ),
            build_task("match-2", date="2026-03-26", time="15:00").model_copy(
                update={"ticket_count": 2}
            ),
            build_task("over-quota", date="2026-03-26", time="15:00").model_copy(
                update={"ticket_count": 1}
            ),
            build_task("second-slot", date="2026-03-26", time="16:30").model_copy(
                update={"ticket_count": 2}
            ),
            build_task("wrong-date", date="2026-03-27", time="15:00"),
        ]
    )

    slots = resolve_trigger_slots(
        AvailabilityTriggerRequest(
            source="availability-api",
            metadata={"event_id": "evt-001"},
            availabilities=[
                {
                    "date": "2026/03/26",
                    "time": "15:00",
                    "quantity": 4,
                },
                {
                    "date": "2026/03/26",
                    "time": "16:30",
                    "quantity": 2,
                },
                {
                    "date": "2026/03/27",
                    "time": "10:00",
                    "quantity": 6,
                },
            ],
        )
    )
    result = store.apply_availability_trigger(
        slots,
        source="availability-api",
        metadata={"event_id": "evt-001"},
    )

    assert sorted(result["matched_task_ids"]) == ["match-1", "match-2", "second-slot"]

    claimed = store.claim_pending_tasks("worker-1", 10)

    assert sorted(task.task_id for task in claimed) == [
        "match-1",
        "match-2",
        "second-slot",
    ]
    assert store.get_task("over-quota").status == "pending"  # type: ignore[union-attr]
    assert store.get_task("wrong-date").status == "pending"  # type: ignore[union-attr]
    assert store.get_task("match-1").metadata["availability_dispatch_availability"] == {  # type: ignore[union-attr]
        "date": "2026-03-26",
        "time": "15:00",
        "quantity": 4,
    }


def test_availability_trigger_survives_external_sheet_resync_for_pending_tasks(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.sync_external_tasks(
        [
            build_task("task-1", date="2026-03-26", time="15:00"),
            build_task("task-2", date="2026-03-26", time="16:00"),
        ]
    )

    slots = resolve_trigger_slots(
        AvailabilityTriggerRequest(
            availabilities=[
                {
                    "date": "2026/03/26",
                    "time": "15:00",
                    "quantity": 1,
                }
            ]
        )
    )
    store.apply_availability_trigger(slots, source="availability-api")
    store.sync_external_tasks(
        [
            build_task("task-1", date="2026-03-26", time="15:00"),
            build_task("task-2", date="2026-03-26", time="16:00"),
        ]
    )

    claimed = store.claim_pending_tasks("worker-1", 10)

    assert [task.task_id for task in claimed] == ["task-1"]


def test_pending_sheet_time_change_reflects_and_clears_old_match(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.sync_external_tasks(
        [
            build_task("task-1", date="2026-03-26", time="15:00"),
        ]
    )
    store.apply_availability_trigger(
        resolve_trigger_slots(
            AvailabilityTriggerRequest(
                availabilities=[
                    {
                        "date": "2026/03/26",
                        "time": "15:00",
                        "quantity": 1,
                    }
                ]
            )
        ),
        source="availability-api",
    )

    store.sync_external_tasks(
        [
            build_task("task-1", date="2026-03-26", time="16:30"),
        ]
    )

    updated = store.get_task("task-1")

    assert updated is not None
    assert updated.time == "16:30"
    assert "availability_dispatch_match" not in updated.metadata
    assert store.claim_pending_tasks("worker-1", 10) == []


def test_apply_availability_trigger_reopens_failed_matching_task(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.upsert_tasks(
        [
            build_task("failed-task", date="2026-03-26", time="15:00").model_copy(
                update={
                    "status": "failed",
                    "assigned_worker": "worker-1",
                    "retry_count": 4,
                    "failure_reason": "Card declined",
                    "stage": "Checkout failed",
                    "upstream_proxy": "http://proxy.invalid",
                    "flaresolverr_url": "http://127.0.0.1:8191/v1",
                    "metadata": {"source": "google_sheets", "sheet_row_number": 2},
                }
            )
        ]
    )

    result = store.apply_availability_trigger(
        resolve_trigger_slots(
            AvailabilityTriggerRequest(
                availabilities=[
                    {
                        "date": "2026/03/26",
                        "time": "15:00",
                        "quantity": 1,
                    }
                ]
            )
        ),
        source="availability-api",
    )

    assert result["matched_task_ids"] == ["failed-task"]
    assert result["reopened_task_ids"] == ["failed-task"]
    reopened = store.get_task("failed-task")
    assert reopened is not None
    assert reopened.status == "pending"
    assert reopened.assigned_worker is None
    assert reopened.retry_count == 0
    assert reopened.failure_reason == ""
    assert reopened.stage == "Reopened by availability trigger"
    assert reopened.upstream_proxy == ""
    assert reopened.flaresolverr_url == ""
    assert store.claim_pending_tasks("worker-1", 10)[0].task_id == "failed-task"


def test_apply_availability_trigger_prioritizes_pending_before_failed_retry(tmp_path) -> None:
    store = build_task_store(tmp_path, require_availability_trigger=True)
    store.upsert_tasks(
        [
            build_task("pending-task", date="2026-03-26", time="15:00"),
            build_task("failed-task", date="2026-03-26", time="15:00").model_copy(
                update={"status": "failed"}
            ),
        ]
    )

    result = store.apply_availability_trigger(
        resolve_trigger_slots(
            AvailabilityTriggerRequest(
                availabilities=[
                    {
                        "date": "2026/03/26",
                        "time": "15:00",
                        "quantity": 1,
                    }
                ]
            )
        ),
        source="availability-api",
    )

    assert result["matched_task_ids"] == ["pending-task"]
    assert result["reopened_task_ids"] == []
    assert store.get_task("pending-task").status == "pending"  # type: ignore[union-attr]
    assert store.get_task("failed-task").status == "failed"  # type: ignore[union-attr]
