from __future__ import annotations

from typing import Any

from alias_manager import VALID_PROVIDERS, create_alias
from shared.models import BookingTask


DEFAULT_WORKER_EMAIL_PROVIDER = "burner"


def resolve_worker_email_provider(raw_provider: str) -> str:
    provider = raw_provider.strip().lower() or DEFAULT_WORKER_EMAIL_PROVIDER
    if provider not in VALID_PROVIDERS:
        supported = ", ".join(VALID_PROVIDERS)
        raise RuntimeError(
            f"Unsupported worker email provider '{provider}'. Supported: {supported}"
        )
    return provider


async def ensure_task_email(
    task: BookingTask,
    email_provider: str,
) -> tuple[BookingTask, dict[str, Any]]:
    existing_email = task.email.strip()
    if existing_email:
        return task, {}

    provider = resolve_worker_email_provider(email_provider)
    resolved_email = await create_alias(
        provider,
        first_name=task.firstName,
        last_name=task.lastName,
    )
    return task.model_copy(update={"email": resolved_email}), {
        "email_generated": True,
        "email_provider": provider,
        "resolved_email": resolved_email,
    }
