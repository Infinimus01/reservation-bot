from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "queued", "assigned", "running", "completed", "failed"]
WorkerStatus = Literal["online", "offline", "draining"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BookingTask(BaseModel):
    task_id: str
    status: TaskStatus = "pending"
    assigned_worker: str | None = None
    firstName: str
    lastName: str
    email: str = ""
    phone: str
    zip: str
    country: str
    date: str
    time: str
    ticket_count: int = 1
    job_time: str = "00:00"
    retry_count: int = 0
    failure_reason: str = ""
    last_updated: datetime = Field(default_factory=utc_now)
    stage: str = ""
    upstream_proxy: str = ""
    flaresolverr_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStatusUpdate(BaseModel):
    worker_id: str
    status: TaskStatus
    stage: str = ""
    failure_reason: str = ""
    flaresolverr_url: str = ""
    upstream_proxy: str = ""
    email: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerRegistration(BaseModel):
    worker_id: str
    worker_name: str
    max_tasks: int
    flaresolverr_urls: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerHeartbeat(BaseModel):
    active_task_ids: list[str] = Field(default_factory=list)
    available_slots: int
    flaresolverr_urls: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerRecord(BaseModel):
    worker_id: str
    worker_name: str
    status: WorkerStatus = "online"
    max_tasks: int
    active_task_count: int = 0
    available_slots: int = 0
    flaresolverr_urls: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=utc_now)
    last_heartbeat: datetime = Field(default_factory=utc_now)


class TaskAssignment(BaseModel):
    task: BookingTask
    assigned_flaresolverr_url: str = ""
    assigned_upstream_proxy: str = ""


class AssignmentRequest(BaseModel):
    requested_slots: int
    flaresolverr_urls: list[str] = Field(default_factory=list)
    active_task_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AvailabilitySlot(BaseModel):
    date: str
    time: str
    quantity: int = Field(gt=0)


class AvailabilityTriggerRequest(BaseModel):
    availabilities: list[AvailabilitySlot] = Field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AvailabilityTriggerResult(BaseModel):
    normalized_availabilities: list[AvailabilitySlot] = Field(default_factory=list)
    matched_task_ids: list[str] = Field(default_factory=list)
    matched_tasks: int = 0
    updated_pending_tasks: int = 0
    published_task_ids: list[str] = Field(default_factory=list)
    published_tasks: int = 0
    publication_errors: dict[str, str] = Field(default_factory=dict)
    task_source: dict[str, Any] = Field(default_factory=dict)


class BookingJobMessage(BaseModel):
    task: BookingTask
    source: str = ""
    queued_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobResultMessage(BaseModel):
    task_id: str
    worker_id: str
    status: TaskStatus
    stage: str = ""
    failure_reason: str = ""
    flaresolverr_url: str = ""
    upstream_proxy: str = ""
    email: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    reported_at: datetime = Field(default_factory=utc_now)
