from __future__ import annotations

import asyncio

import pytest

from shared.models import BookingTask
from worker.email_resolver import ensure_task_email, resolve_worker_email_provider


def build_task(**overrides: object) -> BookingTask:
    defaults: dict[str, object] = {
        "task_id": "task-001",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "email": "",
        "phone": "1234567890",
        "zip": "97220",
        "country": "United States Of America",
        "date": "2026-04-01",
        "time": "09:00",
    }
    defaults.update(overrides)
    return BookingTask(**defaults)


def test_resolve_worker_email_provider_defaults_to_burner() -> None:
    assert resolve_worker_email_provider("") == "burner"


def test_resolve_worker_email_provider_rejects_unknown_provider() -> None:
    with pytest.raises(RuntimeError, match="Unsupported worker email provider"):
        resolve_worker_email_provider("unknown")


def test_ensure_task_email_keeps_existing_email() -> None:
    task = build_task(email="ada@example.com")

    resolved_task, metadata = asyncio.run(ensure_task_email(task, "burner"))

    assert resolved_task.email == "ada@example.com"
    assert metadata == {}


def test_ensure_task_email_generates_missing_email(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create_alias(
        provider: str,
        first_name: str = "",
        last_name: str = "",
    ) -> str:
        assert provider == "burner"
        assert first_name == "Ada"
        assert last_name == "Lovelace"
        return "generated@example.com"

    monkeypatch.setattr("worker.email_resolver.create_alias", fake_create_alias)
    task = build_task()

    resolved_task, metadata = asyncio.run(ensure_task_email(task, "burner"))

    assert resolved_task.email == "generated@example.com"
    assert metadata == {
        "email_generated": True,
        "email_provider": "burner",
        "resolved_email": "generated@example.com",
    }
