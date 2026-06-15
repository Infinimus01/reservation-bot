from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import threading
import time
from typing import Any, Sequence
from urllib.parse import quote

import requests

from master.task_store import TaskStore
from shared.config import GoogleSheetsSettings
from shared.models import BookingTask


logger = logging.getLogger("master.google_sheets")

TASK_HEADER_ALIASES = {
    "task_id": "task_id",
    "taskid": "task_id",
    "id": "task_id",
    "first_name": "firstName",
    "firstname": "firstName",
    "last_name": "lastName",
    "lastname": "lastName",
    "email": "email",
    "phone": "phone",
    "zip": "zip",
    "zipcode": "zip",
    "postal_code": "zip",
    "country": "country",
    "date": "date",
    "time": "time",
    "ticket_count": "ticket_count",
    "ticketcount": "ticket_count",
    "tickets": "ticket_count",
    "job_time": "job_time",
    "jobtime": "job_time",
    "status": "status",
    "assigned_worker": "assigned_worker",
    "assignedworker": "assigned_worker",
    "retry_count": "retry_count",
    "retrycount": "retry_count",
    "failure_reason": "failure_reason",
    "failurereason": "failure_reason",
    "stage": "stage",
    "last_updated": "last_updated",
    "lastupdated": "last_updated",
    "upstream_proxy": "upstream_proxy",
    "upstreamproxy": "upstream_proxy",
    "flaresolverr_url": "flaresolverr_url",
    "flaresolverrurl": "flaresolverr_url",
    "metadata": "metadata",
    "metadata_json": "metadata",
    "dispatch_ready": "dispatch_ready",
    "dispatchready": "dispatch_ready",
    "ready": "ready",
    "enabled": "enabled",
    "availability_met": "availability_met",
    "availabilitymet": "availability_met",
    "available": "available",
}

REQUIRED_TASK_FIELDS = (
    "date",
    "time",
    "ticket_count",
)
DISPATCH_CONTROL_FIELDS = (
    "dispatch_ready",
    "ready",
    "enabled",
    "availability_met",
    "available",
)
TRUTHY_VALUES = {"1", "true", "yes", "y", "on", "ready"}
FALSY_VALUES = {"0", "false", "no", "n", "off", "hold", "paused", "disabled", "skip"}
TASK_STATUSES = {"pending", "assigned", "running", "completed", "failed"}
AUTO_CREATED_SHEET_HEADERS = [
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
]
RUNTIME_SHEET_FIELDS = (
    "email",
    "status",
    "stage",
    "failure_reason",
    "last_updated",
    "assigned_worker",
    "metadata",
)
GENERATED_SHEET_FIELDS = (
    "firstName",
    "lastName",
    "phone",
    "zip",
    "country",
)

DEFAULT_COUNTRY = "United States Of America"
GENERATED_FIRST_NAMES = (
    "James",
    "Emma",
    "Noah",
    "Olivia",
    "Liam",
    "Ava",
    "Mason",
    "Sophia",
    "Ethan",
    "Grace",
    "Henry",
    "Chloe",
)
GENERATED_LAST_NAMES = (
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Wilson",
    "Taylor",
    "Anderson",
    "Clark",
)
US_CONTACT_PROFILES = (
    {"zip": "02108", "area_codes": ("617", "857")},
    {"zip": "10001", "area_codes": ("212", "332", "646", "917")},
    {"zip": "19103", "area_codes": ("215", "267", "445")},
    {"zip": "30303", "area_codes": ("404", "470", "678")},
    {"zip": "33130", "area_codes": ("305", "786")},
    {"zip": "60601", "area_codes": ("312", "773", "872")},
    {"zip": "75201", "area_codes": ("214", "469", "972")},
    {"zip": "77002", "area_codes": ("281", "346", "713", "832")},
    {"zip": "80202", "area_codes": ("303", "720")},
    {"zip": "85004", "area_codes": ("480", "602", "623")},
    {"zip": "90012", "area_codes": ("213", "323", "310", "424")},
    {"zip": "92101", "area_codes": ("619", "858")},
    {"zip": "94105", "area_codes": ("415", "628")},
    {"zip": "97220", "area_codes": ("503", "971")},
    {"zip": "98101", "area_codes": ("206", "253", "425")},
)


def _normalize_header(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return cleaned.strip("_")


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default

    text = str(value).strip().lower()
    if not text:
        return default
    if text in TRUTHY_VALUES:
        return True
    if text in FALSY_VALUES:
        return False
    return default


def _parse_int(value: Any, default: int) -> int:
    if value is None:
        return default

    text = str(value).strip()
    if not text:
        return default

    try:
        return int(text)
    except ValueError:
        return default


def _parse_metadata(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"sheet_metadata_raw": value}

    return parsed if isinstance(parsed, dict) else {"sheet_metadata_raw": value}


def _stable_int(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _stable_choice(options: Sequence[str], *parts: object) -> str:
    if not options:
        raise ValueError("options cannot be empty")
    return options[_stable_int(*parts) % len(options)]


def _stable_digit_string(length: int, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    digits = ""
    while len(digits) < length:
        digits += "".join(char for char in digest if char.isdigit())
        digest = hashlib.sha1(digest.encode("utf-8")).hexdigest()
    return digits[:length]


def _extract_digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def _normalize_us_zip(value: str) -> str:
    digits = _extract_digits(value)
    return digits[:5] if len(digits) >= 5 else ""


def _normalize_us_area_code(value: str) -> str:
    digits = _extract_digits(value)
    return digits[:3] if len(digits) >= 10 else ""


def _find_profile_by_area_code(area_code: str) -> dict[str, Any] | None:
    for profile in US_CONTACT_PROFILES:
        if area_code in profile["area_codes"]:
            return dict(profile)
    return None


def _find_profile_by_zip(zip_code: str) -> dict[str, Any] | None:
    normalized = _normalize_us_zip(zip_code)
    if not normalized:
        return None

    for profile in US_CONTACT_PROFILES:
        if profile["zip"] == normalized:
            return dict(profile)

    for profile in US_CONTACT_PROFILES:
        if profile["zip"][:3] == normalized[:3]:
            return dict(profile)

    for profile in US_CONTACT_PROFILES:
        if profile["zip"][0] == normalized[0]:
            return dict(profile)

    return None


def _select_us_contact_profile(
    identity_key: str,
    zip_value: str,
    phone_value: str,
) -> dict[str, Any]:
    area_code = _normalize_us_area_code(phone_value)
    if area_code:
        by_area = _find_profile_by_area_code(area_code)
        if by_area is not None:
            return by_area

    by_zip = _find_profile_by_zip(zip_value)
    if by_zip is not None:
        return by_zip

    return dict(US_CONTACT_PROFILES[_stable_int(identity_key, "contact_profile") % len(US_CONTACT_PROFILES)])


def _generate_us_phone_number(
    profile: dict[str, Any],
    identity_key: str,
) -> str:
    area_code = _stable_choice(profile["area_codes"], identity_key, "area_code")
    exchange_first = str(2 + (_stable_int(identity_key, "exchange_first") % 8))
    exchange_rest = _stable_digit_string(2, identity_key, "exchange_rest")
    line_number = _stable_digit_string(4, identity_key, "line_number")
    return f"{area_code}{exchange_first}{exchange_rest}{line_number}"


def _apply_generated_defaults(
    mapped: dict[str, str],
    identity_key: str,
) -> dict[str, str]:
    generated_fields: dict[str, str] = {}

    if not mapped.get("firstName", "").strip():
        generated_fields["firstName"] = _stable_choice(
            GENERATED_FIRST_NAMES,
            identity_key,
            "firstName",
        )
        mapped["firstName"] = generated_fields["firstName"]

    if not mapped.get("lastName", "").strip():
        generated_fields["lastName"] = _stable_choice(
            GENERATED_LAST_NAMES,
            identity_key,
            "lastName",
        )
        mapped["lastName"] = generated_fields["lastName"]

    if not mapped.get("country", "").strip():
        generated_fields["country"] = DEFAULT_COUNTRY
        mapped["country"] = generated_fields["country"]

    profile = _select_us_contact_profile(
        identity_key,
        mapped.get("zip", ""),
        mapped.get("phone", ""),
    )

    if not mapped.get("zip", "").strip():
        generated_fields["zip"] = str(profile["zip"])
        mapped["zip"] = generated_fields["zip"]

    if not mapped.get("phone", "").strip():
        generated_fields["phone"] = _generate_us_phone_number(profile, identity_key)
        mapped["phone"] = generated_fields["phone"]

    return generated_fields


class GoogleSheetsTaskSource:
    def __init__(
        self,
        settings: GoogleSheetsSettings,
        task_store: TaskStore,
    ) -> None:
        self.settings = settings
        self.task_store = task_store
        self._lock = threading.Lock()
        self._last_sync_monotonic = 0.0
        self._spreadsheet_id_override = settings.spreadsheet_id
        self._spreadsheet_url_override = (
            self._build_spreadsheet_url(settings.spreadsheet_id)
            if settings.spreadsheet_id
            else ""
        )
        self._worksheet_name = settings.worksheet_name or "Tasks"
        self._range_name = (
            settings.range_name
            if "!" in settings.range_name
            else f"{self._worksheet_name}!{settings.range_name or 'A:Z'}"
        )
        self._header_map_cache: dict[str, int] = {}
        self._last_summary: dict[str, Any] = {
            "enabled": settings.enabled,
            "source": "google_sheets",
        }

    def sync_if_due(self, force: bool = False) -> dict[str, Any]:
        if not self.settings.enabled:
            self._last_summary = {
                "enabled": False,
                "source": "google_sheets",
                "reason": "disabled",
            }
            return dict(self._last_summary)

        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._last_sync_monotonic
                and now - self._last_sync_monotonic < self.settings.sync_interval_seconds
            ):
                return dict(self._last_summary)

            try:
                rows = self._fetch_rows()
                tasks = self._rows_to_tasks(rows)
                synced_tasks = self.task_store.sync_external_tasks(tasks)
                generated_defaults_written = self.write_generated_task_defaults(synced_tasks)
                dispatchable_tasks = sum(
                    1 for task in synced_tasks if self.task_store.is_task_dispatchable(task)
                )
                self._last_summary = {
                    "enabled": True,
                    "source": "google_sheets",
                    "fetched_rows": len(rows),
                    "synced_tasks": len(synced_tasks),
                    "dispatchable_tasks": dispatchable_tasks,
                    "generated_defaults_written": generated_defaults_written,
                    "spreadsheet_id": self._active_spreadsheet_id(),
                    "spreadsheet_url": self._active_spreadsheet_url(),
                }
            except Exception as exc:
                logger.exception("Google Sheets sync failed")
                self._last_summary = {
                    "enabled": True,
                    "source": "google_sheets",
                    "error": str(exc)[:500],
                }
            finally:
                self._last_sync_monotonic = now

        return dict(self._last_summary)

    def _fetch_rows(self) -> list[dict[str, str]]:
        if self.settings.csv_url:
            return self._fetch_rows_from_csv()
        if self._active_spreadsheet_id():
            return self._fetch_rows_from_api()
        self._ensure_spreadsheet_exists()
        return self._fetch_rows_from_api()

    def _fetch_rows_from_csv(self) -> list[dict[str, str]]:
        response = requests.get(
            self.settings.csv_url,
            timeout=self.settings.timeout_seconds,
        )
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        return [{key or "": value or "" for key, value in row.items()} for row in reader]

    def _fetch_rows_from_api(self) -> list[dict[str, str]]:
        spreadsheet_id = self._active_spreadsheet_id()
        if not spreadsheet_id:
            raise RuntimeError("No Google Sheets spreadsheet ID is available")

        if self.settings.credentials_file or self.settings.credentials_json:
            self._ensure_existing_sheet_ready(spreadsheet_id)

        encoded_range = quote(self._range_name, safe="!:$'")
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{encoded_range}"
        )

        response: requests.Response
        if self.settings.credentials_file or self.settings.credentials_json:
            response = self._authorized_request("GET", url, scopes=self._readonly_scopes())
        elif self.settings.api_key:
            response = requests.get(
                url,
                params={"key": self.settings.api_key},
                timeout=self.settings.timeout_seconds,
            )
        else:
            raise RuntimeError(
                "Google Sheets API access requires either service account credentials "
                "or GOOGLE_SHEETS_API_KEY"
            )

        response.raise_for_status()
        payload = response.json()
        values = payload.get("values", [])
        return self._sheet_values_to_rows(values)

    def _authorized_request(
        self,
        method: str,
        url: str,
        *,
        scopes: list[str],
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "google-auth is required for authenticated Google Sheets access"
            ) from exc

        if self.settings.credentials_file is not None:
            credentials = Credentials.from_service_account_file(
                str(self.settings.credentials_file),
                scopes=scopes,
            )
        else:
            credentials = Credentials.from_service_account_info(
                json.loads(self.settings.credentials_json),
                scopes=scopes,
            )

        session = AuthorizedSession(credentials)
        try:
            return session.request(
                method,
                url,
                json=json_payload,
                params=params,
                timeout=self.settings.timeout_seconds,
            )
        finally:
            session.close()

    def _readonly_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    def _write_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/spreadsheets"]

    def _sheet_values_to_rows(self, values: list[list[Any]]) -> list[dict[str, str]]:
        if not values:
            return []

        headers = [str(item).strip() for item in values[0]]
        if not any(headers):
            return []

        rows: list[dict[str, str]] = []
        for row in values[1:]:
            mapped_row: dict[str, str] = {}
            for index, header in enumerate(headers):
                mapped_row[header] = str(row[index]).strip() if index < len(row) else ""
            rows.append(mapped_row)
        return rows

    def _rows_to_tasks(self, rows: list[dict[str, str]]) -> list[BookingTask]:
        tasks: list[BookingTask] = []
        for row_number, row in enumerate(rows, start=2):
            task = self._row_to_task(row_number=row_number, row=row)
            if task is not None:
                tasks.append(task)
        return tasks

    def _ensure_spreadsheet_exists(self) -> None:
        if self.settings.csv_url or self._active_spreadsheet_id():
            return
        if not (self.settings.credentials_file or self.settings.credentials_json):
            raise RuntimeError(
                "Google Sheets auto-creation requires GOOGLE_SHEETS_CREDENTIALS_FILE "
                "or GOOGLE_SHEETS_CREDENTIALS_JSON"
            )

        create_url = "https://sheets.googleapis.com/v4/spreadsheets"
        title_suffix = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        spreadsheet_title = f"{self.settings.spreadsheet_title} {title_suffix}".strip()
        create_payload = {
            "properties": {"title": spreadsheet_title},
            "sheets": [{"properties": {"title": self._worksheet_name}}],
        }
        create_response = self._authorized_request(
            "POST",
            create_url,
            scopes=self._write_scopes(),
            json_payload=create_payload,
        )
        create_response.raise_for_status()
        create_result = create_response.json()

        spreadsheet_id = str(create_result["spreadsheetId"]).strip()
        spreadsheet_url = str(
            create_result.get("spreadsheetUrl") or self._build_spreadsheet_url(spreadsheet_id)
        ).strip()

        header_range = (
            f"{self._worksheet_name}!A1:{self._column_letters(len(AUTO_CREATED_SHEET_HEADERS))}1"
        )
        update_url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{quote(header_range, safe='!:$')}"
        )
        update_response = self._authorized_request(
            "PUT",
            update_url,
            scopes=self._write_scopes(),
            params={"valueInputOption": "RAW"},
            json_payload={
                "range": header_range,
                "majorDimension": "ROWS",
                "values": [AUTO_CREATED_SHEET_HEADERS],
            },
        )
        update_response.raise_for_status()

        self._spreadsheet_id_override = spreadsheet_id
        self._spreadsheet_url_override = spreadsheet_url
        logger.info("Created Google Sheets task sheet: %s", spreadsheet_url)
        print(f"Created Google Sheets task sheet: {spreadsheet_url}", flush=True)

    def _ensure_existing_sheet_ready(self, spreadsheet_id: str) -> None:
        metadata_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        metadata_response = self._authorized_request(
            "GET",
            metadata_url,
            scopes=self._readonly_scopes(),
            params={"fields": "sheets.properties.title"},
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        sheet_titles = {
            str(sheet.get("properties", {}).get("title", "")).strip()
            for sheet in metadata.get("sheets", [])
        }

        if self._worksheet_name not in sheet_titles:
            batch_update_url = f"{metadata_url}:batchUpdate"
            add_sheet_response = self._authorized_request(
                "POST",
                batch_update_url,
                scopes=self._write_scopes(),
                json_payload={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {"title": self._worksheet_name}
                            }
                        }
                    ]
                },
            )
            add_sheet_response.raise_for_status()
            logger.info(
                "Created Google Sheets worksheet '%s' in %s",
                self._worksheet_name,
                self._build_spreadsheet_url(spreadsheet_id),
            )

        self._ensure_runtime_headers(spreadsheet_id)

    def write_task_runtime_state(self, task: BookingTask) -> bool:
        return self.write_task_runtime_states([task]) > 0

    def write_generated_task_defaults(self, tasks: list[BookingTask]) -> int:
        if not (self.settings.credentials_file or self.settings.credentials_json):
            return 0

        spreadsheet_id = self._active_spreadsheet_id()
        if not spreadsheet_id:
            return 0

        header_map = self._ensure_runtime_headers(spreadsheet_id)
        data: list[dict[str, Any]] = []
        updated_task_count = 0
        for task in tasks:
            if task.metadata.get("source") != "google_sheets":
                continue

            row_number = task.metadata.get("sheet_row_number")
            if not isinstance(row_number, int) or row_number < 2:
                continue

            raw_generated_fields = task.metadata.get("sheet_generated_fields")
            if not isinstance(raw_generated_fields, list):
                continue

            generated_fields = [
                field_name
                for field_name in raw_generated_fields
                if field_name in GENERATED_SHEET_FIELDS
            ]
            if not generated_fields:
                continue

            field_values = {
                "firstName": task.firstName,
                "lastName": task.lastName,
                "phone": task.phone,
                "zip": task.zip,
                "country": task.country,
            }

            wrote_any = False
            for field_name in generated_fields:
                column_index = header_map.get(field_name)
                if column_index is None:
                    continue

                value = str(field_values.get(field_name, "")).strip()
                if not value:
                    continue

                data.append(
                    {
                        "range": (
                            f"{self._worksheet_name}!"
                            f"{self._column_letters(column_index)}{row_number}"
                        ),
                        "majorDimension": "ROWS",
                        "values": [[value]],
                    }
                )
                wrote_any = True

            if wrote_any:
                updated_task_count += 1

        self._batch_update_sheet_ranges(spreadsheet_id, data)
        return updated_task_count

    def write_task_runtime_states(self, tasks: list[BookingTask]) -> int:
        if not (self.settings.credentials_file or self.settings.credentials_json):
            return 0

        spreadsheet_id = self._active_spreadsheet_id()
        if not spreadsheet_id:
            return 0

        header_map = self._ensure_runtime_headers(spreadsheet_id)
        data: list[dict[str, Any]] = []
        updated_task_count = 0
        for task in tasks:
            if task.metadata.get("source") != "google_sheets":
                continue

            row_number = task.metadata.get("sheet_row_number")
            if not isinstance(row_number, int) or row_number < 2:
                continue

            metadata_payload = dict(task.metadata)
            metadata_payload["sheet_runtime_status_managed"] = True
            resolved_email = task.email.strip()
            if not resolved_email:
                metadata_email = metadata_payload.get("resolved_email")
                if isinstance(metadata_email, str):
                    resolved_email = metadata_email.strip()

            runtime_values = {
                "email": resolved_email,
                "status": task.status,
                "stage": task.stage,
                "failure_reason": task.failure_reason,
                "last_updated": task.last_updated.isoformat(),
                "assigned_worker": task.assigned_worker or "",
                "metadata": json.dumps(metadata_payload, sort_keys=True, default=str),
            }

            for field_name, value in runtime_values.items():
                column_index = header_map.get(field_name)
                if column_index is None:
                    continue
                data.append(
                    {
                        "range": (
                            f"{self._worksheet_name}!"
                            f"{self._column_letters(column_index)}{row_number}"
                        ),
                        "majorDimension": "ROWS",
                        "values": [[value]],
                    }
                )
            updated_task_count += 1

        self._batch_update_sheet_ranges(spreadsheet_id, data)
        return updated_task_count

    def _batch_update_sheet_ranges(
        self,
        spreadsheet_id: str,
        data: list[dict[str, Any]],
    ) -> None:
        if not data:
            return

        batch_update_url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values:batchUpdate"
        )
        response = self._authorized_request(
            "POST",
            batch_update_url,
            scopes=self._write_scopes(),
            params={"valueInputOption": "RAW"},
            json_payload={"data": data},
        )
        response.raise_for_status()

    def _ensure_runtime_headers(self, spreadsheet_id: str) -> dict[str, int]:
        headers = self._fetch_header_row(spreadsheet_id)
        if not headers:
            headers = list(AUTO_CREATED_SHEET_HEADERS)
            self._write_header_row(spreadsheet_id, headers)
            logger.info(
                "Initialized Google Sheets headers in %s (%s)",
                self._worksheet_name,
                self._build_spreadsheet_url(spreadsheet_id),
            )
        else:
            header_map = self._build_header_map(headers)
            missing_headers = [
                header
                for header in AUTO_CREATED_SHEET_HEADERS
                if TASK_HEADER_ALIASES.get(_normalize_header(header), header) not in header_map
            ]
            if missing_headers:
                headers = [*headers, *missing_headers]
                self._write_header_row(spreadsheet_id, headers)
                logger.info(
                    "Extended Google Sheets headers in %s with runtime columns",
                    self._worksheet_name,
                )

        self._header_map_cache = self._build_header_map(headers)
        return dict(self._header_map_cache)

    def _fetch_header_row(self, spreadsheet_id: str) -> list[str]:
        header_range = f"{self._worksheet_name}!A1:ZZ1"
        header_url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{quote(header_range, safe='!:$')}"
        )
        header_response = self._authorized_request(
            "GET",
            header_url,
            scopes=self._readonly_scopes(),
        )
        header_response.raise_for_status()
        header_values = header_response.json().get("values", [])
        if not header_values:
            return []
        return [str(cell).strip() for cell in header_values[0] if str(cell).strip()]

    def _write_header_row(self, spreadsheet_id: str, headers: list[str]) -> None:
        header_range = (
            f"{self._worksheet_name}!A1:{self._column_letters(len(headers))}1"
        )
        header_url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{quote(header_range, safe='!:$')}"
        )
        update_response = self._authorized_request(
            "PUT",
            header_url,
            scopes=self._write_scopes(),
            params={"valueInputOption": "RAW"},
            json_payload={
                "range": header_range,
                "majorDimension": "ROWS",
                "values": [headers],
            },
        )
        update_response.raise_for_status()

    def _build_header_map(self, headers: list[str]) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for index, header in enumerate(headers, start=1):
            canonical = TASK_HEADER_ALIASES.get(_normalize_header(header))
            if canonical:
                mapping[canonical] = index
        return mapping

    def _active_spreadsheet_id(self) -> str:
        return self._spreadsheet_id_override or self.settings.spreadsheet_id

    def _active_spreadsheet_url(self) -> str:
        if self.settings.csv_url:
            return self.settings.csv_url
        return self._spreadsheet_url_override or self._build_spreadsheet_url(
            self._active_spreadsheet_id()
        )

    def _build_spreadsheet_url(self, spreadsheet_id: str) -> str:
        spreadsheet_id = spreadsheet_id.strip()
        if not spreadsheet_id:
            return ""
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    def _column_letters(self, index: int) -> str:
        result = ""
        current = index
        while current > 0:
            current, remainder = divmod(current - 1, 26)
            result = chr(ord("A") + remainder) + result
        return result

    def _generated_task_id(self, row_number: int) -> str:
        source_key = self._active_spreadsheet_id() or self.settings.csv_url or "google_sheets"
        scope = f"{source_key}:{self._range_name}"
        digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:12]
        return f"gsheet-{digest}-row-{row_number}"

    def _row_to_task(
        self,
        row_number: int,
        row: dict[str, str],
    ) -> BookingTask | None:
        if not any(str(value).strip() for value in row.values()):
            return None

        mapped: dict[str, str] = {}
        extra_columns: dict[str, str] = {}
        for raw_key, raw_value in row.items():
            normalized_key = _normalize_header(raw_key)
            canonical_key = TASK_HEADER_ALIASES.get(normalized_key)
            value = str(raw_value).strip()
            if canonical_key:
                mapped[canonical_key] = value
            elif value:
                extra_columns[raw_key] = value

        ticket_count = _parse_int(mapped.get("ticket_count"), default=0)
        missing_fields = [field for field in REQUIRED_TASK_FIELDS if not mapped.get(field)]
        invalid_fields: list[str] = []
        if "ticket_count" not in missing_fields and ticket_count <= 0:
            invalid_fields.append("ticket_count")

        if invalid_fields:
            logger.warning(
                "Skipping Google Sheets row %s; invalid fields: %s",
                row_number,
                ", ".join(invalid_fields),
            )
            return None

        if missing_fields:
            logger.warning(
                "Skipping Google Sheets row %s; missing fields: %s",
                row_number,
                ", ".join(missing_fields),
            )
            return None

        provided_task_id = mapped.get("task_id", "")
        task_id = provided_task_id or self._generated_task_id(row_number)
        generated_fields = _apply_generated_defaults(mapped, task_id)
        metadata = _parse_metadata(mapped.get("metadata", ""))
        runtime_status_managed = bool(metadata.get("sheet_runtime_status_managed"))
        dispatch_controls_present = False
        sheet_dispatch_ready = True
        for field_name in DISPATCH_CONTROL_FIELDS:
            if field_name in mapped:
                dispatch_controls_present = True
                sheet_dispatch_ready = sheet_dispatch_ready and _parse_bool(
                    mapped[field_name],
                    default=True,
                )

        status_text = mapped.get("status", "").strip().lower()
        if not runtime_status_managed and (
            status_text in FALSY_VALUES
            or (status_text in TASK_STATUSES and status_text != "pending")
        ):
            sheet_dispatch_ready = False
        status = (
            "pending"
            if runtime_status_managed
            else status_text if status_text in TASK_STATUSES else "pending"
        )

        metadata.update(
            {
                "source": "google_sheets",
                "sheet_row_number": row_number,
                "sheet_task_id_mode": "provided" if provided_task_id else "generated",
                "sheet_dispatch_ready": (
                    sheet_dispatch_ready if dispatch_controls_present or status_text else True
                ),
            }
        )
        if status_text:
            metadata["sheet_status"] = status_text
        if extra_columns:
            metadata["sheet_extra"] = extra_columns
        if generated_fields:
            metadata["sheet_generated_fields"] = sorted(generated_fields)

        return BookingTask(
            task_id=task_id,
            status=status,
            assigned_worker=mapped.get("assigned_worker") or None,
            firstName=mapped["firstName"],
            lastName=mapped["lastName"],
            email=mapped.get("email", ""),
            phone=mapped["phone"],
            zip=mapped["zip"],
            country=mapped["country"],
            date=mapped["date"],
            time=mapped["time"],
            ticket_count=ticket_count,
            job_time=mapped.get("job_time") or "00:00",
            retry_count=_parse_int(mapped.get("retry_count"), default=0),
            failure_reason=mapped.get("failure_reason", ""),
            stage=mapped.get("stage", ""),
            upstream_proxy=mapped.get("upstream_proxy", ""),
            flaresolverr_url=mapped.get("flaresolverr_url", ""),
            metadata=metadata,
        )
