from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager
import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status

from master.availability import resolve_trigger_slots
from master.google_sheets import GoogleSheetsTaskSource
from master.queue_dispatcher import AvailabilityQueueDispatcher
from master.task_store import TaskStore
from shared.config import MasterSettings, resolve_local_rabbitmq_management_url
from shared.models import (
    AvailabilityTriggerRequest,
    AvailabilityTriggerResult,
    BookingTask,
)
from shared.rabbitmq import RabbitMQPublisher, ensure_broker_ready


load_dotenv()
settings = MasterSettings.from_env()
task_store = TaskStore(
    settings.state_db_path,
    require_availability_trigger=settings.require_availability_trigger,
)
google_sheets = GoogleSheetsTaskSource(settings.google_sheets, task_store)
publisher = RabbitMQPublisher(settings.rabbitmq)
queue_dispatcher = AvailabilityQueueDispatcher(
    task_store=task_store,
    publisher=publisher,
    task_source=google_sheets,
)
logger = logging.getLogger("master.main")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


def startup() -> None:
    task_store.initialize()
    expired = task_store.expire_past_date_tasks()
    if expired:
        logger.info("Expired %d past-date task(s) on startup: %s", len(expired), expired)
        # Write expired status back to Google Sheet immediately on startup
        expired_tasks = [t for t in task_store.list_tasks() if t.task_id in set(expired)]
        if expired_tasks:
            try:
                google_sheets.write_task_runtime_states(expired_tasks)
            except Exception:
                logger.exception("Failed to write startup-expired task statuses to sheet")
    ensure_broker_ready(settings.rabbitmq)
    queue_dispatcher.refresh_task_source(force=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    yield


app = FastAPI(
    title="Distributed Flare Bot Master",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, object]:
    task_source = queue_dispatcher.refresh_task_source()
    return {
        "status": "ok",
        "task_source": task_source,
        "tasks": len(task_store.list_tasks()),
        "broker": {
            "booking_queue": settings.rabbitmq.booking_queue,
            "results_queue": settings.rabbitmq.results_queue,
            "retry_queue": settings.rabbitmq.booking_retry_queue,
            "management_url": resolve_local_rabbitmq_management_url(),
        },
    }


@app.get("/tasks", response_model=list[BookingTask], dependencies=[Depends(require_api_key)])
def list_tasks(status: str | None = None) -> list[BookingTask]:
    queue_dispatcher.refresh_task_source()
    return task_store.list_tasks(status)


@app.post("/tasks", response_model=list[BookingTask], dependencies=[Depends(require_api_key)])
def create_tasks(tasks: list[BookingTask]) -> list[BookingTask]:
    return task_store.upsert_tasks(tasks)


@app.post("/tasks/sync", dependencies=[Depends(require_api_key)])
def sync_tasks() -> dict[str, object]:
    return queue_dispatcher.refresh_task_source(force=True)


@app.post(
    "/availability/trigger",
    response_model=AvailabilityTriggerResult,
    dependencies=[Depends(require_api_key)],
)
def trigger_availability(
    request: AvailabilityTriggerRequest,
) -> AvailabilityTriggerResult:
    task_source_summary = queue_dispatcher.refresh_task_source(force=True)
    expired = task_store.expire_past_date_tasks()
    if expired:
        logger.info("Expired %d past-date tasks during trigger: %s", len(expired), expired)
    try:
        normalized_slots = resolve_trigger_slots(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dispatch_result = queue_dispatcher.dispatch_matching_tasks(
        normalized_slots,
        source=request.source,
        metadata=request.metadata,
    )
    task_source_summary["dispatchable_tasks"] = sum(
        1 for task in task_store.list_tasks("pending") if task_store.is_task_dispatchable(task)
    )
    return AvailabilityTriggerResult(
        normalized_availabilities=normalized_slots,
        matched_task_ids=[str(task_id) for task_id in dispatch_result["matched_task_ids"]],
        matched_tasks=len(dispatch_result["matched_task_ids"]),
        updated_pending_tasks=int(dispatch_result["updated_pending_tasks"]),
        published_task_ids=[
            str(task_id) for task_id in dispatch_result.get("published_task_ids", [])
        ],
        published_tasks=int(dispatch_result.get("published_tasks", 0)),
        publication_errors={
            str(task_id): str(error)
            for task_id, error in dispatch_result.get("publication_errors", {}).items()
        },
        task_source=task_source_summary,
    )


if __name__ == "__main__":
    uvicorn.run(
        "master.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
