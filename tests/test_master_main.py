from __future__ import annotations

from types import SimpleNamespace

import master.main as master_main


def test_health_includes_rabbitmq_management_url(monkeypatch) -> None:
    monkeypatch.setattr(
        master_main,
        "settings",
        SimpleNamespace(
            rabbitmq=SimpleNamespace(
                booking_queue="booking_jobs",
                results_queue="job_results",
                booking_retry_queue="booking_jobs.retry",
            )
        ),
    )
    monkeypatch.setattr(
        master_main,
        "queue_dispatcher",
        SimpleNamespace(refresh_task_source=lambda: {"source": "google_sheets"}),
    )
    monkeypatch.setattr(
        master_main,
        "task_store",
        SimpleNamespace(list_tasks=lambda: ["task-1", "task-2"]),
    )
    monkeypatch.setattr(
        master_main,
        "resolve_local_rabbitmq_management_url",
        lambda: "http://127.0.0.1:32772",
    )

    response = master_main.health()

    assert response["broker"]["management_url"] == "http://127.0.0.1:32772"
    assert response["tasks"] == 2
