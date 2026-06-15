from __future__ import annotations

from datetime import datetime, timezone
import io
from contextlib import redirect_stdout

from master.google_sheets import AUTO_CREATED_SHEET_HEADERS, GoogleSheetsTaskSource
from master.task_store import TaskStore
from shared.config import GoogleSheetsSettings
from shared.models import BookingTask


def build_google_sheets_settings(**overrides: object) -> GoogleSheetsSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "spreadsheet_id": "",
        "spreadsheet_title": "Selenium Bot Tasks",
        "worksheet_name": "Tasks",
        "range_name": "Tasks!A:Z",
        "csv_url": "https://example.invalid/tasks.csv",
        "api_key": "",
        "credentials_file": None,
        "credentials_json": "",
        "sync_interval_seconds": 15,
        "timeout_seconds": 15,
    }
    defaults.update(overrides)
    return GoogleSheetsSettings(**defaults)


def build_task_store(tmp_path) -> TaskStore:
    store = TaskStore(tmp_path / "master_state.db")
    store.initialize()
    return store


AUTO_CREATED_HEADER_ROW = list(AUTO_CREATED_SHEET_HEADERS)


def test_google_sheets_sync_creates_dispatchable_tasks(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Email": "ada@example.com",
            "Phone": "1234567890",
            "Zip": "97220",
            "Country": "United States Of America",
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "2",
            "Dispatch Ready": "yes",
            "Notes": "priority",
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 1
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_id.startswith("gsheet-")
    assert task.task_id.endswith("-row-2")
    assert task.firstName == "Ada"
    assert task.ticket_count == 2
    assert task.metadata["source"] == "google_sheets"
    assert task.metadata["sheet_task_id_mode"] == "generated"
    assert task.metadata["sheet_dispatch_ready"] is True
    assert task.metadata["sheet_extra"] == {"Notes": "priority"}


def test_google_sheets_sync_allows_blank_email(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Phone": "1234567890",
            "Zip": "97220",
            "Country": "United States Of America",
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "1",
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 1
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].email == ""


def test_google_sheets_creates_sheet_when_source_missing(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(
        build_google_sheets_settings(
            spreadsheet_id="",
            csv_url="",
            credentials_json='{"type":"service_account"}',
        ),
        store,
    )

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    calls: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = []

    def fake_authorized_request(
        method: str,
        url: str,
        *,
        scopes: list[str],
        json_payload: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> FakeResponse:
        calls.append((method, url, json_payload, params))
        if method == "POST" and url.endswith("/v4/spreadsheets"):
            return FakeResponse(
                {
                    "spreadsheetId": "spreadsheet-123",
                    "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/spreadsheet-123/edit",
                }
            )
        if method == "GET" and url.endswith("/v4/spreadsheets/spreadsheet-123"):
            return FakeResponse({"sheets": [{"properties": {"title": "Tasks"}}]})
        if method == "GET" and "/values/Tasks!A1:ZZ1" in url:
            return FakeResponse(
                {
                    "values": [AUTO_CREATED_HEADER_ROW]
                }
            )
        if method == "PUT" and "/values/" in url:
            return FakeResponse({"updatedRange": "Tasks!A1:Q1"})
        if method == "GET" and "/values/" in url:
            return FakeResponse({"values": [AUTO_CREATED_HEADER_ROW]})
        raise AssertionError(f"Unexpected request: {method} {url}")

    source._authorized_request = fake_authorized_request  # type: ignore[method-assign]
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        summary = source.sync_if_due(force=True)

    assert summary["spreadsheet_id"] == "spreadsheet-123"
    assert summary["spreadsheet_url"] == (
        "https://docs.google.com/spreadsheets/d/spreadsheet-123/edit"
    )
    assert "Created Google Sheets task sheet:" in stdout.getvalue()
    assert any(
        call[0] == "PUT"
        and call[2] is not None
        and call[2]["values"][0][0] == "first_name"
        for call in calls
    )


def test_google_sheets_initializes_headers_for_existing_empty_sheet(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(
        build_google_sheets_settings(
            spreadsheet_id="spreadsheet-456",
            csv_url="",
            credentials_json='{"type":"service_account"}',
        ),
        store,
    )

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    header_put_calls: list[dict[str, object]] = []

    def fake_authorized_request(
        method: str,
        url: str,
        *,
        scopes: list[str],
        json_payload: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> FakeResponse:
        if method == "GET" and url.endswith("/v4/spreadsheets/spreadsheet-456"):
            return FakeResponse({"sheets": [{"properties": {"title": "Tasks"}}]})
        if method == "GET" and "/values/Tasks!A1:ZZ1" in url:
            return FakeResponse({})
        if method == "PUT" and "/values/Tasks!A1:Q1" in url:
            header_put_calls.append(json_payload or {})
            return FakeResponse({"updatedRange": "Tasks!A1:Q1"})
        if method == "GET" and "/values/Tasks!A:Z" in url:
            return FakeResponse({"values": [AUTO_CREATED_HEADER_ROW]})
        raise AssertionError(f"Unexpected request: {method} {url}")

    source._authorized_request = fake_authorized_request  # type: ignore[method-assign]
    summary = source.sync_if_due(force=True)

    assert summary["spreadsheet_id"] == "spreadsheet-456"
    assert summary["synced_tasks"] == 0
    assert len(header_put_calls) == 1
    assert header_put_calls[0]["values"][0] == AUTO_CREATED_HEADER_ROW


def test_runtime_managed_sheet_status_does_not_block_dispatch(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Phone": "1234567890",
            "Zip": "97220",
            "Country": "United States Of America",
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "1",
            "Dispatch Ready": "yes",
            "Status": "running",
            "Metadata": '{"sheet_runtime_status_managed": true}',
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 1
    task = store.list_tasks()[0]
    assert task.status == "pending"
    assert task.metadata["sheet_status"] == "running"
    assert task.metadata["sheet_runtime_status_managed"] is True
    assert store.is_task_dispatchable(task) is True


def test_google_sheets_write_task_runtime_state_updates_sheet_row(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(
        build_google_sheets_settings(
            spreadsheet_id="spreadsheet-789",
            csv_url="",
            credentials_json='{"type":"service_account"}',
        ),
        store,
    )

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    existing_headers = [
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
        "metadata",
    ]
    header_put_calls: list[dict[str, object]] = []
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
            return FakeResponse({"values": [existing_headers]})
        if method == "PUT" and "/values/Tasks!A1:Q1" in url:
            header_put_calls.append(json_payload or {})
            return FakeResponse({"updatedRange": "Tasks!A1:Q1"})
        if method == "POST" and url.endswith("/values:batchUpdate"):
            batch_update_calls.append(json_payload or {})
            return FakeResponse({"totalUpdatedCells": 6})
        raise AssertionError(f"Unexpected request: {method} {url}")

    source._authorized_request = fake_authorized_request  # type: ignore[method-assign]
    task = BookingTask(
        task_id="gsheet-runtime-row-2",
        status="failed",
        assigned_worker="worker-1",
        firstName="Ada",
        lastName="Lovelace",
        email="",
        phone="1234567890",
        zip="97220",
        country="United States Of America",
        date="2026-04-01",
        time="09:00",
        failure_reason="Card declined",
        stage="Checkout failed",
        last_updated=datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
        metadata={
            "source": "google_sheets",
            "sheet_row_number": 2,
            "sheet_dispatch_ready": True,
            "resolved_email": "ada.bot@example.com",
        },
    )

    updated = source.write_task_runtime_state(task)

    assert updated is True
    assert len(header_put_calls) == 1
    assert header_put_calls[0]["values"][0] == [
        *existing_headers,
        "stage",
        "failure_reason",
        "last_updated",
        "assigned_worker",
    ]
    assert len(batch_update_calls) == 1
    cell_updates = {
        entry["range"]: entry["values"][0][0]
        for entry in batch_update_calls[0]["data"]
    }
    assert cell_updates["Tasks!C2"] == "ada.bot@example.com"
    assert cell_updates["Tasks!L2"] == "failed"
    assert cell_updates["Tasks!N2"] == "Checkout failed"
    assert cell_updates["Tasks!O2"] == "Card declined"
    assert cell_updates["Tasks!P2"] == "2026-03-25T12:00:00+00:00"
    assert cell_updates["Tasks!Q2"] == "worker-1"
    metadata_payload = cell_updates["Tasks!M2"]
    assert '"sheet_runtime_status_managed": true' in metadata_payload
    assert '"sheet_row_number": 2' in metadata_payload


def test_generated_task_id_is_stable_across_repeated_syncs(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Email": "ada@example.com",
            "Phone": "1234567890",
            "Zip": "97220",
            "Country": "United States Of America",
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "1",
        }
    ]

    source.sync_if_due(force=True)
    first_task = store.list_tasks()[0]

    source.sync_if_due(force=True)
    tasks = store.list_tasks()

    assert len(tasks) == 1
    assert tasks[0].task_id == first_task.task_id


def test_google_sheets_sync_generates_missing_identity_defaults(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "2",
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 1
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.firstName
    assert task.lastName
    assert task.country == "United States Of America"
    assert len(task.zip) == 5
    assert task.zip.isdigit()
    assert len(task.phone) == 10
    assert task.phone.isdigit()
    assert task.metadata["sheet_generated_fields"] == [
        "country",
        "firstName",
        "lastName",
        "phone",
        "zip",
    ]

    source.sync_if_due(force=True)
    synced_task = store.list_tasks()[0]
    assert synced_task.firstName == task.firstName
    assert synced_task.lastName == task.lastName
    assert synced_task.zip == task.zip
    assert synced_task.phone == task.phone


def test_google_sheets_sync_writes_generated_defaults_back_to_sheet(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(
        build_google_sheets_settings(
            spreadsheet_id="spreadsheet-generated",
            csv_url="",
            credentials_json='{"type":"service_account"}',
        ),
        store,
    )

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
            return FakeResponse({"values": [AUTO_CREATED_HEADER_ROW]})
        if method == "POST" and url.endswith("/values:batchUpdate"):
            batch_update_calls.append(json_payload or {})
            return FakeResponse({"totalUpdatedCells": 5})
        raise AssertionError(f"Unexpected request: {method} {url}")

    source._authorized_request = fake_authorized_request  # type: ignore[method-assign]
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "Date": "2026-04-01",
            "Time": "09:00",
            "Ticket Count": "2",
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 1
    assert summary["generated_defaults_written"] == 1
    assert len(batch_update_calls) == 1

    task = store.list_tasks()[0]
    cell_updates = {
        entry["range"]: entry["values"][0][0]
        for entry in batch_update_calls[0]["data"]
    }
    assert cell_updates["Tasks!A2"] == task.firstName
    assert cell_updates["Tasks!B2"] == task.lastName
    assert cell_updates["Tasks!D2"] == task.phone
    assert cell_updates["Tasks!E2"] == task.zip
    assert cell_updates["Tasks!F2"] == task.country


def test_google_sheets_requires_ticket_count_date_and_time(tmp_path) -> None:
    store = build_task_store(tmp_path)
    source = GoogleSheetsTaskSource(build_google_sheets_settings(), store)
    source._fetch_rows = lambda: [  # type: ignore[method-assign]
        {
            "Date": "2026-04-01",
            "Time": "09:00",
        }
    ]

    summary = source.sync_if_due(force=True)

    assert summary["synced_tasks"] == 0
    assert store.list_tasks() == []


def test_sync_external_tasks_preserves_non_pending_runtime_state(tmp_path) -> None:
    store = build_task_store(tmp_path)
    store.upsert_tasks(
        [
            BookingTask(
                task_id="task-002",
                status="running",
                assigned_worker="worker-1",
                firstName="Grace",
                lastName="Hopper",
                email="grace@example.com",
                phone="1234567890",
                zip="97220",
                country="United States Of America",
                date="2026-04-01",
                time="09:00",
                stage="Worker launched bot process",
                last_updated=datetime.utcnow(),
                metadata={"instance_id": 7},
            )
        ]
    )

    store.sync_external_tasks(
        [
            BookingTask(
                task_id="task-002",
                status="pending",
                firstName="Changed",
                lastName="Name",
                email="changed@example.com",
                phone="9999999999",
                zip="11111",
                country="Canada",
                date="2026-05-01",
                time="10:00",
                metadata={
                    "source": "google_sheets",
                    "sheet_dispatch_ready": False,
                },
            )
        ]
    )

    task = store.get_task("task-002")
    assert task is not None
    assert task.status == "running"
    assert task.assigned_worker == "worker-1"
    assert task.firstName == "Grace"
    assert task.email == "grace@example.com"
    assert task.metadata["instance_id"] == 7
    assert task.metadata["sheet_dispatch_ready"] is False


def test_claim_pending_tasks_skips_dispatch_blocked_tasks(tmp_path) -> None:
    store = build_task_store(tmp_path)
    store.upsert_tasks(
        [
            BookingTask(
                task_id="blocked-task",
                firstName="Blocked",
                lastName="User",
                email="blocked@example.com",
                phone="1234567890",
                zip="97220",
                country="United States Of America",
                date="2026-04-01",
                time="09:00",
                last_updated=datetime(2026, 1, 1, 0, 0, 0),
                metadata={"sheet_dispatch_ready": False},
            ),
            BookingTask(
                task_id="ready-task",
                firstName="Ready",
                lastName="User",
                email="ready@example.com",
                phone="1234567890",
                zip="97220",
                country="United States Of America",
                date="2026-04-01",
                time="09:00",
                last_updated=datetime(2026, 1, 1, 0, 0, 1),
                metadata={"sheet_dispatch_ready": True},
            ),
        ]
    )

    claimed = store.claim_pending_tasks("worker-1", 1)

    assert [task.task_id for task in claimed] == ["ready-task"]
    blocked = store.get_task("blocked-task")
    ready = store.get_task("ready-task")
    assert blocked is not None and blocked.status == "pending"
    assert ready is not None and ready.status == "assigned"
