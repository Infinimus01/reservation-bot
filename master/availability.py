from __future__ import annotations

from datetime import datetime
import re

from shared.models import AvailabilitySlot, AvailabilityTriggerRequest


_ORDINAL_SUFFIX_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_availability_date_value(raw_value: str) -> str:
    text = str(raw_value).strip()
    if not text:
        raise ValueError("Availability date cannot be empty")

    try:
        return datetime.strptime(text, "%Y/%m/%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Unsupported availability date format: {raw_value!r}. "
            "Use YYYY/MM/DD format like '2026/03/26'."
        ) from exc


def normalize_task_date_value(raw_value: str) -> str:
    text = _WHITESPACE_RE.sub(
        " ",
        _ORDINAL_SUFFIX_RE.sub("", str(raw_value).strip().replace(",", " ")),
    ).strip()
    if not text:
        raise ValueError("Task date cannot be empty")

    for date_format in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(
        f"Unsupported task date format: {raw_value!r}. "
        "Use YYYY-MM-DD format like '2026-03-26'."
    )


def normalize_time_value(raw_value: str) -> str:
    text = str(raw_value).strip()
    if not text:
        raise ValueError("Availability time cannot be empty")

    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(
            f"Unsupported availability time format: {raw_value!r}. "
            "Use HH:MM in 24-hour format like '15:00'."
        ) from exc


def normalize_slot(slot: AvailabilitySlot) -> AvailabilitySlot:
    return AvailabilitySlot(
        date=normalize_availability_date_value(slot.date),
        time=normalize_time_value(slot.time),
        quantity=slot.quantity,
    )


def resolve_trigger_slots(request: AvailabilityTriggerRequest) -> list[AvailabilitySlot]:
    raw_slots = list(request.availabilities)
    if not raw_slots:
        raise ValueError("Availability trigger requires a non-empty availabilities list")

    normalized_slots: list[AvailabilitySlot] = []
    merged_quantities: dict[tuple[str, str], int] = {}
    for slot in raw_slots:
        normalized = normalize_slot(slot)
        key = (normalized.date, normalized.time)
        merged_quantities[key] = merged_quantities.get(key, 0) + normalized.quantity

    for (date, time), quantity in sorted(merged_quantities.items()):
        normalized_slots.append(
            AvailabilitySlot(date=date, time=time, quantity=quantity)
        )
    return normalized_slots
