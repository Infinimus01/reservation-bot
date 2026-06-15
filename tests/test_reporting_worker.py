from __future__ import annotations

from datetime import datetime, timezone

from master.google_sheets import GoogleSheetsTaskSource
from master.reporting_worker import ReportingBatcher
from master.task_store import TaskStore
from shared.config import GoogleSheetsSettings
from shared.models import BookingTask, JobResultMessage


def build_google_sheets_settings(**overrides: object) -> GoogleSheetsSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "spreadsheet_id": "spreadsheet-123",
        "spreadsheet_title": "Selenium Bot Tasks",
        "worksheet_name": "Tasks",
        "range_name": "Tasks!A:Z",
        "csv_url": "",
        "api_key": "",
        "credentials_file": None,
        "credentials_json": '{"type":"service_account"}',
        "sync_interval_seconds": 15,
        "timeout_seconds": 15,
    }
    defaults.update(overrides)
    return GoogleSheetsSettings(**defaults)


def build_sheet_task(task_id: str, row_number: int) -> BookingTask:
    return BookingTask(
        task_id=task_id,
        firstName="Ada",
        lastName="Lovelace",
        email=f"{task_id}@example.com",
        phone="1234567890",
        zip="97220",
        country="United States Of America",
        date="2026-04-01",
        time="09:00",
        metadata={
            "source": "google_sheets",
            "sheet_row_number": row_number,
            "sheet_dispatch_ready": True,
        },
    )


def test_reporting_batcher_batches_google_sheets_updates(tmp_path) -> None:
    store = TaskStore(tmp_path / "master_state.db")
    store.initialize()
    tasks = [build_sheet_task(f"task-{index}", index + 2) for index in range(50)]
    store.upsert_tasks(tasks)

    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    batch_update_calls: list[dict[str, object]] = []

    def fake_authorized_request(
        method: str,
        url: str,
        *,
        scopes: list[str],
        json_payload: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> FakeResponse:
        if method == "GET" and "/values/Tasks!A1:ZZ1" in url:
            return FakeResponse(
                {
                    "values": [[
                        "first_name",
                        "last_name",
                        "email",
                        "phone",
                        "zip",
                        "country",
                        "date",
                        "time",
                        "ticket_count",
                        "job_time",
                        "dispatch_ready",
                        "status",
                        "stage",
                        "failure_reason",
                        "last_updated",
                        "assigned_worker",
                        "metadata",
                    ]]
                }
            )
        if method == "POST" and url.endswith("/values:batchUpdate"):
            batch_update_calls.append(json_payload or {})
            return FakeResponse({"totalUpdatedCells": 350})
        raise AssertionError(f"Unexpected request: {method} {url}")

    source._authorized_request = fake_authorized_request  # type: ignore[method-assign]
    batcher = ReportingBatcher(store, source)
    results = [
        JobResultMessage(
            task_id=f"task-{index}",
            worker_id="worker-a",
            status="completed",
            stage="Completed",
            email=f"task-{index}@example.com",
            reported_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        )
        for index in range(50)
    ]

    updated_task_ids = batcher.flush(results)

    assert len(updated_task_ids) == 50
    assert len(batch_update_calls) == 1
    assert len(batch_update_calls[0]["data"]) == 50 * 7
