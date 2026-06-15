from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from master.availability import normalize_task_date_value, normalize_time_value
from shared.models import AvailabilitySlot, BookingTask, TaskStatusUpdate


class TaskStore:
    def __init__(
        self,
        db_path: Path,
        require_availability_trigger: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.require_availability_trigger = require_availability_trigger
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    assigned_worker TEXT,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    zip TEXT NOT NULL,
                    country TEXT NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    ticket_count INTEGER NOT NULL,
                    job_time TEXT NOT NULL,
                    retry_count INTEGER NOT NULL,
                    failure_reason TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    upstream_proxy TEXT NOT NULL,
                    flaresolverr_url TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, last_updated)"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_worker_status
                ON tasks(assigned_worker, status)
                """
            )

    def upsert_tasks(self, tasks: Iterable[BookingTask]) -> list[BookingTask]:
        task_list = list(tasks)
        if not task_list:
            return []

        with self._lock, self._connection() as conn:
            for task in task_list:
                self._upsert_task(conn, task)
        return task_list

    def sync_external_tasks(self, tasks: Iterable[BookingTask]) -> list[BookingTask]:
        task_list = list(tasks)
        if not task_list:
            return []

        synced: list[BookingTask] = []
        with self._lock, self._connection() as conn:
            for task in task_list:
                existing_row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                existing = self._task_from_row(existing_row) if existing_row else None
                merged_task = self._merge_external_task(existing, task)
                self._upsert_task(conn, merged_task)
                synced.append(merged_task)
        return synced

    def list_tasks(self, status: str | None = None) -> list[BookingTask]:
        query = "SELECT * FROM tasks"
        params: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY last_updated ASC"

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> BookingTask | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def get_tasks(self, task_ids: Iterable[str]) -> list[BookingTask]:
        ordered_ids = [str(task_id) for task_id in task_ids if str(task_id).strip()]
        if not ordered_ids:
            return []

        found: dict[str, BookingTask] = {}
        with self._connection() as conn:
            placeholders = ", ".join("?" for _ in ordered_ids)
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE task_id IN ({placeholders})",
                tuple(ordered_ids),
            ).fetchall()
        for row in rows:
            task = self._task_from_row(row)
            found[task.task_id] = task
        return [found[task_id] for task_id in ordered_ids if task_id in found]

    def claim_pending_tasks(self, worker_id: str, limit: int) -> list[BookingTask]:
        if limit <= 0:
            return []

        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE status = 'pending'
                ORDER BY last_updated ASC
                """
            ).fetchall()

            now_iso = datetime.utcnow().isoformat()
            claimed_ids: list[str] = []
            for row in rows:
                task = self._task_from_row(row)
                if not self.is_task_dispatchable(task):
                    continue
                claimed_ids.append(task.task_id)
                if len(claimed_ids) >= limit:
                    break

            for task_id in claimed_ids:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'assigned',
                        assigned_worker = ?,
                        last_updated = ?,
                        failure_reason = '',
                        stage = 'Assigned by master'
                    WHERE task_id = ?
                    """,
                    (worker_id, now_iso, task_id),
                )

        return [task for task in (self.get_task(task_id) for task_id in claimed_ids) if task]

    def is_task_dispatchable(self, task: BookingTask) -> bool:
        if task.status != "pending":
            return False

        if not self._metadata_flag_is_true(
            task.metadata.get("sheet_dispatch_ready"),
            default=True,
        ):
            return False

        availability_match = task.metadata.get("availability_dispatch_match")
        if availability_match is not None:
            return self._metadata_flag_is_true(availability_match, default=False)

        return not self.require_availability_trigger

    def apply_availability_trigger(
        self,
        slots: Iterable[AvailabilitySlot],
        source: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_slots = list(slots)
        matched_ids: list[str] = []
        reopened_ids: list[str] = []
        slot_quantities = {
            (slot.date, slot.time): slot.quantity for slot in normalized_slots
        }
        remaining_quantities = dict(slot_quantities)

        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE status IN ('pending', 'failed')
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                         last_updated ASC
                """
            ).fetchall()

            for row in rows:
                task = self._task_from_row(row)
                matched_slot = self._matching_availability_slot(task, normalized_slots)
                next_metadata = dict(task.metadata)
                updated_at_iso = datetime.utcnow().isoformat()
                next_metadata["availability_dispatch_source"] = source.strip()
                next_metadata["availability_dispatch_metadata"] = dict(metadata or {})
                next_metadata["availability_dispatch_updated_at"] = updated_at_iso

                if matched_slot is not None:
                    slot_key = (matched_slot.date, matched_slot.time)
                    remaining_quantity = remaining_quantities.get(slot_key, 0)
                    sheet_ready = self._metadata_flag_is_true(
                        task.metadata.get("sheet_dispatch_ready"),
                        default=True,
                    )
                    can_dispatch = sheet_ready and task.ticket_count <= remaining_quantity
                    next_metadata["availability_dispatch_match"] = can_dispatch
                    next_metadata["availability_dispatch_availability"] = (
                        matched_slot.model_dump()
                    )
                    next_metadata["availability_dispatch_remaining_before_claim"] = (
                        remaining_quantity
                    )
                    if can_dispatch:
                        remaining_quantities[slot_key] = remaining_quantity - task.ticket_count
                        next_metadata["availability_dispatch_remaining_after_claim"] = (
                            remaining_quantities[slot_key]
                        )
                        matched_ids.append(task.task_id)
                    else:
                        next_metadata["availability_dispatch_remaining_after_claim"] = (
                            remaining_quantity
                        )
                else:
                    next_metadata["availability_dispatch_match"] = False
                    next_metadata.pop("availability_dispatch_availability", None)
                    next_metadata.pop("availability_dispatch_remaining_before_claim", None)
                    next_metadata.pop("availability_dispatch_remaining_after_claim", None)

                if task.status == "failed" and next_metadata.get("availability_dispatch_match") is True:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'pending',
                            assigned_worker = NULL,
                            retry_count = 0,
                            failure_reason = '',
                            stage = 'Reopened by availability trigger',
                            last_updated = ?,
                            upstream_proxy = '',
                            flaresolverr_url = '',
                            metadata_json = ?
                        WHERE task_id = ?
                        """,
                        (
                            updated_at_iso,
                            json.dumps(next_metadata),
                            task.task_id,
                        ),
                    )
                    reopened_ids.append(task.task_id)
                else:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET metadata_json = ?
                        WHERE task_id = ?
                        """,
                        (json.dumps(next_metadata), task.task_id),
                    )

        return {
            "updated_pending_tasks": len(rows),
            "matched_task_ids": matched_ids,
            "reopened_task_ids": reopened_ids,
        }

    def update_assignment(
        self,
        task_id: str,
        worker_id: str,
        flaresolverr_url: str = "",
    ) -> BookingTask | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET assigned_worker = ?,
                    flaresolverr_url = ?,
                    last_updated = ?
                WHERE task_id = ?
                """,
                (worker_id, flaresolverr_url, datetime.utcnow().isoformat(), task_id),
            )
        return self.get_task(task_id)

    def update_task_status(
        self,
        task_id: str,
        update: TaskStatusUpdate,
    ) -> BookingTask | None:
        current = self.get_task(task_id)
        if current is None:
            return None

        next_metadata = dict(current.metadata)
        next_metadata.update(update.metadata)
        next_email = current.email
        if update.email.strip():
            next_email = update.email.strip()
        elif not next_email:
            metadata_email = next_metadata.get("resolved_email")
            if isinstance(metadata_email, str) and metadata_email.strip():
                next_email = metadata_email.strip()

        next_task = current.model_copy(
            update={
                "status": update.status,
                "assigned_worker": update.worker_id,
                "email": next_email,
                "stage": update.stage or current.stage,
                "failure_reason": update.failure_reason,
                "flaresolverr_url": update.flaresolverr_url or current.flaresolverr_url,
                "upstream_proxy": update.upstream_proxy or current.upstream_proxy,
                "metadata": next_metadata,
                "last_updated": datetime.utcnow(),
            }
        )
        self.upsert_tasks([next_task])
        return self.get_task(task_id)

    def mark_task_queued(
        self,
        task_id: str,
        *,
        broker_message_id: str,
        broker_source: str,
        metadata: dict[str, object] | None = None,
    ) -> BookingTask | None:
        current = self.get_task(task_id)
        if current is None:
            return None

        next_metadata = dict(current.metadata)
        next_metadata.update(metadata or {})
        next_metadata["broker_message_id"] = broker_message_id
        next_metadata["broker_source"] = broker_source
        next_metadata["broker_queued_at"] = datetime.utcnow().isoformat()

        next_task = current.model_copy(
            update={
                "status": "queued",
                "assigned_worker": None,
                "failure_reason": "",
                "stage": "Queued in RabbitMQ",
                "flaresolverr_url": "",
                "metadata": next_metadata,
                "last_updated": datetime.utcnow(),
            }
        )
        self.upsert_tasks([next_task])
        return self.get_task(task_id)

    def expire_past_date_tasks(self) -> list[str]:
        """Mark any pending/queued/failed task whose booking date has passed as failed."""
        from datetime import date as _date
        today_str = _date.today().isoformat()
        expired_ids: list[str] = []
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT task_id, date FROM tasks
                WHERE status IN ('pending', 'queued', 'failed')
                """
            ).fetchall()
            now_iso = datetime.utcnow().isoformat()
            for row in rows:
                task_date = str(row["date"]).strip().replace("/", "-")
                try:
                    if task_date < today_str:
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status = 'failed',
                                failure_reason = 'Booking date has passed',
                                stage = 'Expired',
                                last_updated = ?
                            WHERE task_id = ?
                            """,
                            (now_iso, row["task_id"]),
                        )
                        expired_ids.append(row["task_id"])
                except Exception:
                    continue
        return expired_ids

    def requeue_tasks_for_worker(
        self,
        worker_id: str,
        reason: str,
        max_retries: int,
    ) -> list[BookingTask]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE assigned_worker = ?
                  AND status IN ('assigned', 'running')
                """,
                (worker_id,),
            ).fetchall()
        return self._requeue_rows(rows, reason, max_retries)

    def requeue_stale_tasks(
        self,
        stale_before: datetime,
        reason: str,
        max_retries: int,
    ) -> list[BookingTask]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('assigned', 'running')
                  AND last_updated < ?
                """,
                (stale_before.isoformat(),),
            ).fetchall()
        return self._requeue_rows(rows, reason, max_retries)

    def _requeue_rows(
        self,
        rows: list[sqlite3.Row],
        reason: str,
        max_retries: int,
    ) -> list[BookingTask]:
        if not rows:
            return []

        updated_ids: list[str] = []
        with self._lock, self._connection() as conn:
            for row in rows:
                task = self._task_from_row(row)
                next_retry = task.retry_count + 1
                status = "pending" if next_retry <= max_retries else "failed"
                assigned_worker = None if status == "pending" else task.assigned_worker
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        assigned_worker = ?,
                        retry_count = ?,
                        failure_reason = ?,
                        stage = ?,
                        last_updated = ?,
                        flaresolverr_url = ''
                    WHERE task_id = ?
                    """,
                    (
                        status,
                        assigned_worker,
                        next_retry,
                        reason,
                        "Requeued by master" if status == "pending" else "Retry limit reached",
                        datetime.utcnow().isoformat(),
                        task.task_id,
                    ),
                )
                updated_ids.append(task.task_id)
        return [
            task
            for task in (self.get_task(task_id) for task_id in updated_ids)
            if task is not None
        ]

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _upsert_task(self, conn: sqlite3.Connection, task: BookingTask) -> None:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, status, assigned_worker, first_name, last_name,
                email, phone, zip, country, date, time, ticket_count,
                job_time, retry_count, failure_reason, last_updated, stage,
                upstream_proxy, flaresolverr_url, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status = excluded.status,
                assigned_worker = excluded.assigned_worker,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                email = excluded.email,
                phone = excluded.phone,
                zip = excluded.zip,
                country = excluded.country,
                date = excluded.date,
                time = excluded.time,
                ticket_count = excluded.ticket_count,
                job_time = excluded.job_time,
                retry_count = excluded.retry_count,
                failure_reason = excluded.failure_reason,
                last_updated = excluded.last_updated,
                stage = excluded.stage,
                upstream_proxy = excluded.upstream_proxy,
                flaresolverr_url = excluded.flaresolverr_url,
                metadata_json = excluded.metadata_json
            """,
            self._task_to_params(task),
        )

    def _merge_external_task(
        self,
        existing: BookingTask | None,
        incoming: BookingTask,
    ) -> BookingTask:
        if existing is None:
            return incoming

        merged_metadata = dict(existing.metadata)
        merged_metadata.update(incoming.metadata)

        if existing.status == "pending":
            if self._dispatch_slot_changed(existing, incoming):
                self._clear_availability_dispatch_metadata(merged_metadata)
            return incoming.model_copy(
                update={
                    "status": existing.status,
                    "assigned_worker": existing.assigned_worker,
                    "retry_count": existing.retry_count,
                    "failure_reason": existing.failure_reason,
                    "last_updated": existing.last_updated,
                    "stage": existing.stage,
                    "metadata": merged_metadata,
                }
            )

        return existing.model_copy(update={"metadata": merged_metadata})

    @staticmethod
    def _dispatch_slot_changed(existing: BookingTask, incoming: BookingTask) -> bool:
        return (
            existing.date != incoming.date
            or existing.time != incoming.time
            or existing.ticket_count != incoming.ticket_count
        )

    @staticmethod
    def _clear_availability_dispatch_metadata(metadata: dict[str, object]) -> None:
        for key in list(metadata):
            if key.startswith("availability_dispatch_"):
                metadata.pop(key, None)

    def _task_to_params(self, task: BookingTask) -> tuple[object, ...]:
        return (
            task.task_id,
            task.status,
            task.assigned_worker,
            task.firstName,
            task.lastName,
            task.email,
            task.phone,
            task.zip,
            task.country,
            task.date,
            task.time,
            task.ticket_count,
            task.job_time,
            task.retry_count,
            task.failure_reason,
            task.last_updated.isoformat(),
            task.stage,
            task.upstream_proxy,
            task.flaresolverr_url,
            json.dumps(task.metadata),
        )

    def _task_from_row(self, row: sqlite3.Row) -> BookingTask:
        return BookingTask(
            task_id=row["task_id"],
            status=row["status"],
            assigned_worker=row["assigned_worker"],
            firstName=row["first_name"],
            lastName=row["last_name"],
            email=row["email"],
            phone=row["phone"],
            zip=row["zip"],
            country=row["country"],
            date=row["date"],
            time=row["time"],
            ticket_count=row["ticket_count"],
            job_time=row["job_time"],
            retry_count=row["retry_count"],
            failure_reason=row["failure_reason"],
            last_updated=datetime.fromisoformat(row["last_updated"]),
            stage=row["stage"],
            upstream_proxy=row["upstream_proxy"],
            flaresolverr_url=row["flaresolverr_url"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _metadata_flag_is_true(value: object, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "ready"}

    @staticmethod
    def _matching_availability_slot(
        task: BookingTask,
        slots: list[AvailabilitySlot],
    ) -> AvailabilitySlot | None:
        try:
            normalized_task_date = normalize_task_date_value(task.date)
            normalized_task_time = normalize_time_value(task.time)
        except ValueError:
            return None

        for slot in slots:
            if slot.date != normalized_task_date:
                continue
            if slot.time and slot.time != normalized_task_time:
                continue
            return slot
        return None
