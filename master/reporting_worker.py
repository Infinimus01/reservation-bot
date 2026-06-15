from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import time
from typing import Iterable

from dotenv import load_dotenv

from master.google_sheets import GoogleSheetsTaskSource
from master.task_store import TaskStore
from shared.config import MasterSettings
from shared.models import JobResultMessage, TaskStatusUpdate
from shared.rabbitmq import open_channel


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("master.reporting_worker")


class ReportingBatcher:
    def __init__(
        self,
        task_store: TaskStore,
        google_sheets: GoogleSheetsTaskSource,
    ) -> None:
        self.task_store = task_store
        self.google_sheets = google_sheets

    def flush(self, results: Iterable[JobResultMessage]) -> list[str]:
        updated_tasks = []
        updated_task_ids: list[str] = []

        for result in results:
            task = self.task_store.update_task_status(
                result.task_id,
                TaskStatusUpdate(
                    worker_id=result.worker_id,
                    status=result.status,
                    stage=result.stage,
                    failure_reason=result.failure_reason,
                    flaresolverr_url=result.flaresolverr_url,
                    upstream_proxy=result.upstream_proxy,
                    email=result.email,
                    metadata={
                        **result.metadata,
                        "result_reported_at": result.reported_at.isoformat(),
                    },
                ),
            )
            if task is None:
                continue
            updated_tasks.append(task)
            updated_task_ids.append(task.task_id)

        if updated_tasks:
            self.google_sheets.write_task_runtime_states(updated_tasks)

        return updated_task_ids


class ReportingWorker:
    def __init__(self, settings: MasterSettings) -> None:
        self.settings = settings
        self.task_store = TaskStore(
            settings.state_db_path,
            require_availability_trigger=settings.require_availability_trigger,
        )
        self.google_sheets = GoogleSheetsTaskSource(settings.google_sheets, self.task_store)
        self.batcher = ReportingBatcher(self.task_store, self.google_sheets)

    def run(self) -> None:
        self.task_store.initialize()
        pending_results: list[JobResultMessage] = []
        pending_tags: list[int] = []
        last_flush = time.monotonic()

        with open_channel(self.settings.rabbitmq) as channel:
            logger.info(
                "Reporting worker consuming from %s with flush interval %ss",
                self.settings.rabbitmq.results_queue,
                self.settings.reporting_flush_interval_seconds,
            )
            while True:
                method_frame, _properties, body = channel.basic_get(
                    queue=self.settings.rabbitmq.results_queue,
                    auto_ack=False,
                )
                if method_frame is not None and body is not None:
                    pending_results.append(JobResultMessage.model_validate_json(body))
                    pending_tags.append(method_frame.delivery_tag)

                now = time.monotonic()
                should_flush = (
                    pending_results
                    and (
                        now - last_flush >= self.settings.reporting_flush_interval_seconds
                        or len(pending_results) >= 250
                        or method_frame is None
                    )
                )
                if should_flush:
                    updated_task_ids = self.batcher.flush(pending_results)
                    for delivery_tag in pending_tags:
                        channel.basic_ack(delivery_tag)
                    logger.info(
                        "Flushed %d result message(s) covering %d task(s)",
                        len(pending_results),
                        len(updated_task_ids),
                    )
                    pending_results = []
                    pending_tags = []
                    last_flush = now

                if method_frame is None:
                    time.sleep(0.25)


def main() -> None:
    load_dotenv()
    settings = MasterSettings.from_env()
    ReportingWorker(settings).run()


if __name__ == "__main__":
    main()
