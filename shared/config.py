from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


LEGACY_DEFAULT_FLARESOLVERR_URLS: list[str] = [
    "http://localhost:8191/v1",
    "http://localhost:8192/v1",
    "http://localhost:8193/v1",
    "http://localhost:8194/v1",
]
DEFAULT_RABBITMQ_URL = "amqp://guest:guest@127.0.0.1:5672/%2F"


def _env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def build_master_base_url(env: Mapping[str, str] | None = None) -> str:
    runtime_env = _env(env)
    explicit = runtime_env.get("MASTER_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    host = runtime_env.get("MASTER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(runtime_env.get("MASTER_PORT", "8000"))
    return f"http://{host}:{port}"


def split_csv_urls(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_local_flaresolverr_urls(
    host: str = "localhost",
    base_port: int = 8191,
    count: int = 4,
) -> list[str]:
    if count <= 0:
        return []
    return [f"http://{host}:{base_port + offset}/v1" for offset in range(count)]


def _extract_published_port(ports_text: str, container_port: int) -> int | None:
    matches = re.findall(rf"(\d+)->{container_port}/tcp", ports_text)
    if not matches:
        return None
    return int(matches[0])


def _docker_port_candidates(
    container_port: int,
    env: Mapping[str, str] | None = None,
) -> list[tuple[int, int]]:
    runtime_env = _env(env)
    container_hint = runtime_env.get("RABBITMQ_DOCKER_CONTAINER_NAME", "").strip().lower()
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Ports}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    candidates: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        name, image, ports_text = (part.strip() for part in parts)
        lowered_name = name.lower()
        if container_hint and container_hint not in lowered_name:
            continue
        host_port = _extract_published_port(ports_text, container_port)
        if host_port is None:
            continue
        score = 0
        if container_hint:
            score += 100
            if lowered_name == container_hint:
                score += 20
        if "rabbitmq" in lowered_name:
            score += 10
        if "rabbitmq" in image.lower():
            score += 10
        candidates.append((score, host_port))
    return sorted(candidates, key=lambda candidate: (-candidate[0], candidate[1]))


def resolve_local_rabbitmq_port(
    env: Mapping[str, str] | None = None,
    *,
    container_port: int = 5672,
) -> int | None:
    candidates = _docker_port_candidates(container_port, env)
    if not candidates:
        return None
    return candidates[0][1]


def build_local_rabbitmq_url(port: int) -> str:
    return f"amqp://guest:guest@127.0.0.1:{port}/%2F"


def resolve_local_rabbitmq_management_url(
    env: Mapping[str, str] | None = None,
) -> str | None:
    port = resolve_local_rabbitmq_port(env, container_port=15672)
    if port is None:
        return None
    return f"http://127.0.0.1:{port}"


def resolve_rabbitmq_url(env: Mapping[str, str] | None = None) -> str:
    runtime_env = _env(env)
    configured = runtime_env.get("RABBITMQ_URL", DEFAULT_RABBITMQ_URL).strip()
    if configured and configured.lower() != "auto":
        return configured
    resolved_port = resolve_local_rabbitmq_port(runtime_env)
    if resolved_port is None:
        return DEFAULT_RABBITMQ_URL
    return build_local_rabbitmq_url(resolved_port)


@dataclass(frozen=True)
class ResolvedFlaresolverrPool:
    urls: list[str]
    source: str


@dataclass(frozen=True)
class RabbitMQSettings:
    url: str
    booking_exchange: str
    booking_routing_key: str
    booking_queue: str
    booking_retry_exchange: str
    booking_retry_routing_key: str
    booking_retry_queue: str
    booking_retry_delay_ms: int
    booking_max_retries: int
    results_exchange: str
    results_routing_key: str
    results_queue: str
    worker_prefetch_count: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RabbitMQSettings:
        runtime_env = _env(env)
        return cls(
            url=resolve_rabbitmq_url(runtime_env),
            booking_exchange=runtime_env.get(
                "RABBITMQ_BOOKING_EXCHANGE",
                "selenium_bot.booking",
            ).strip()
            or "selenium_bot.booking",
            booking_routing_key=runtime_env.get(
                "RABBITMQ_BOOKING_ROUTING_KEY",
                "booking_jobs",
            ).strip()
            or "booking_jobs",
            booking_queue=runtime_env.get(
                "RABBITMQ_BOOKING_QUEUE",
                "booking_jobs",
            ).strip()
            or "booking_jobs",
            booking_retry_exchange=runtime_env.get(
                "RABBITMQ_BOOKING_RETRY_EXCHANGE",
                "selenium_bot.booking.retry",
            ).strip()
            or "selenium_bot.booking.retry",
            booking_retry_routing_key=runtime_env.get(
                "RABBITMQ_BOOKING_RETRY_ROUTING_KEY",
                "booking_jobs.retry",
            ).strip()
            or "booking_jobs.retry",
            booking_retry_queue=runtime_env.get(
                "RABBITMQ_BOOKING_RETRY_QUEUE",
                "booking_jobs.retry",
            ).strip()
            or "booking_jobs.retry",
            booking_retry_delay_ms=int(
                runtime_env.get("RABBITMQ_BOOKING_RETRY_DELAY_MS", "5000")
            ),
            booking_max_retries=int(
                runtime_env.get("RABBITMQ_BOOKING_MAX_RETRIES", "3")
            ),
            results_exchange=runtime_env.get(
                "RABBITMQ_RESULTS_EXCHANGE",
                "selenium_bot.results",
            ).strip()
            or "selenium_bot.results",
            results_routing_key=runtime_env.get(
                "RABBITMQ_RESULTS_ROUTING_KEY",
                "job_results",
            ).strip()
            or "job_results",
            results_queue=runtime_env.get(
                "RABBITMQ_RESULTS_QUEUE",
                "job_results",
            ).strip()
            or "job_results",
            worker_prefetch_count=max(
                int(runtime_env.get("RABBITMQ_WORKER_PREFETCH_COUNT", "3")),
                1,
            ),
        )


def resolve_flaresolverr_pool(
    explicit_url: str | None = None,
    explicit_urls: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    allow_generated_local_urls: bool = True,
    allow_legacy_fallback: bool = True,
) -> ResolvedFlaresolverrPool:
    runtime_env = _env(env)

    if explicit_url and explicit_url.strip():
        return ResolvedFlaresolverrPool(
            urls=[explicit_url.strip()],
            source="explicit_url",
        )

    if explicit_urls:
        cleaned = [url.strip() for url in explicit_urls if url and url.strip()]
        if cleaned:
            return ResolvedFlaresolverrPool(
                urls=cleaned,
                source="explicit_urls",
            )

    env_urls = split_csv_urls(runtime_env.get("FLARESOLVERR_URLS"))
    if env_urls:
        return ResolvedFlaresolverrPool(urls=env_urls, source="env_urls")

    if allow_generated_local_urls:
        host = runtime_env.get("FLARESOLVERR_HOST", "localhost").strip() or "localhost"
        base_port = int(runtime_env.get("FLARESOLVERR_BASE_PORT", "8191"))
        instance_count = int(runtime_env.get("FLARESOLVERR_INSTANCE_COUNT", "4"))
        generated = build_local_flaresolverr_urls(
            host=host,
            base_port=base_port,
            count=instance_count,
        )
        if generated:
            return ResolvedFlaresolverrPool(urls=generated, source="generated_local")

    if allow_legacy_fallback:
        return ResolvedFlaresolverrPool(
            urls=list(LEGACY_DEFAULT_FLARESOLVERR_URLS),
            source="legacy_fallback",
        )

    return ResolvedFlaresolverrPool(urls=[], source="none")


def get_flaresolverr_urls(
    explicit_url: str | None = None,
    explicit_urls: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    allow_generated_local_urls: bool = True,
    allow_legacy_fallback: bool = True,
) -> list[str]:
    return resolve_flaresolverr_pool(
        explicit_url=explicit_url,
        explicit_urls=explicit_urls,
        env=env,
        allow_generated_local_urls=allow_generated_local_urls,
        allow_legacy_fallback=allow_legacy_fallback,
    ).urls


@dataclass(frozen=True)
class GoogleSheetsSettings:
    enabled: bool
    spreadsheet_id: str
    spreadsheet_title: str
    worksheet_name: str
    range_name: str
    csv_url: str
    api_key: str
    credentials_file: Path | None
    credentials_json: str
    sync_interval_seconds: int
    timeout_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GoogleSheetsSettings:
        runtime_env = _env(env)
        worksheet_name = runtime_env.get("GOOGLE_SHEETS_WORKSHEET", "").strip()
        range_name = runtime_env.get("GOOGLE_SHEETS_RANGE", "").strip()
        if not range_name:
            range_name = f"{worksheet_name}!A:Z" if worksheet_name else "A:Z"

        credentials_file_raw = runtime_env.get(
            "GOOGLE_SHEETS_CREDENTIALS_FILE",
            "",
        ).strip()
        credentials_file = (
            Path(credentials_file_raw).expanduser().resolve()
            if credentials_file_raw
            else None
        )
        csv_url = runtime_env.get("GOOGLE_SHEETS_CSV_URL", "").strip()
        spreadsheet_id = runtime_env.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()

        return cls(
            enabled=parse_bool(
                runtime_env.get("GOOGLE_SHEETS_ENABLED"),
                default=bool(
                    csv_url
                    or spreadsheet_id
                    or credentials_file
                    or runtime_env.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
                ),
            ),
            spreadsheet_id=spreadsheet_id,
            spreadsheet_title=runtime_env.get(
                "GOOGLE_SHEETS_TITLE",
                "Selenium Bot Tasks",
            ).strip()
            or "Selenium Bot Tasks",
            worksheet_name=worksheet_name,
            range_name=range_name,
            csv_url=csv_url,
            api_key=runtime_env.get("GOOGLE_SHEETS_API_KEY", "").strip(),
            credentials_file=credentials_file,
            credentials_json=runtime_env.get(
                "GOOGLE_SHEETS_CREDENTIALS_JSON",
                "",
            ).strip(),
            sync_interval_seconds=int(
                runtime_env.get("GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS", "15")
            ),
            timeout_seconds=int(runtime_env.get("GOOGLE_SHEETS_TIMEOUT_SECONDS", "15")),
        )


@dataclass(frozen=True)
class MasterSettings:
    host: str
    port: int
    api_key: str
    state_db_path: Path
    require_availability_trigger: bool
    reporting_flush_interval_seconds: int
    rabbitmq: RabbitMQSettings
    google_sheets: GoogleSheetsSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MasterSettings:
        runtime_env = _env(env)
        return cls(
            host=runtime_env.get("MASTER_HOST", "0.0.0.0"),
            port=int(runtime_env.get("MASTER_PORT", "8000")),
            api_key=runtime_env.get("MASTER_API_KEY", "").strip(),
            state_db_path=Path(
                runtime_env.get("MASTER_STATE_DB", "master_state.db")
            ).resolve(),
            require_availability_trigger=parse_bool(
                runtime_env.get("MASTER_REQUIRE_AVAILABILITY_TRIGGER"),
                default=False,
            ),
            reporting_flush_interval_seconds=int(
                runtime_env.get("REPORTING_FLUSH_INTERVAL_SECONDS", "15")
            ),
            rabbitmq=RabbitMQSettings.from_env(runtime_env),
            google_sheets=GoogleSheetsSettings.from_env(runtime_env),
        )


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str
    worker_name: str
    email_provider: str
    max_tasks: int
    status_poll_interval_seconds: float
    idle_shutdown_seconds: int
    flaresolverr_startup_timeout_seconds: int
    flaresolverr_startup_poll_seconds: int
    flaresolverr_count: int
    flaresolverr_host: str
    flaresolverr_base_port: int
    flaresolverr_discovery_mode: str
    flaresolverr_docker_label: str
    flaresolverr_docker_image: str
    flaresolverr_container_prefix: str
    autostart_flaresolverr: bool
    rabbitmq: RabbitMQSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        runtime_env = _env(env)
        default_worker_id = runtime_env.get("WORKER_ID") or platform.node() or "worker"
        default_worker_name = runtime_env.get("WORKER_NAME") or default_worker_id
        return cls(
            worker_id=default_worker_id,
            worker_name=default_worker_name,
            email_provider=runtime_env.get("WORKER_EMAIL_PROVIDER", "burner")
            .strip()
            .lower()
            or "burner",
            max_tasks=int(runtime_env.get("WORKER_MAX_TASKS", "10")),
            status_poll_interval_seconds=float(
                runtime_env.get("WORKER_STATUS_POLL_INTERVAL_SECONDS", "1")
            ),
            idle_shutdown_seconds=int(
                runtime_env.get("WORKER_IDLE_SHUTDOWN_SECONDS", "0")
            ),
            flaresolverr_startup_timeout_seconds=int(
                runtime_env.get("WORKER_FLARESOLVERR_STARTUP_TIMEOUT_SECONDS", "90")
            ),
            flaresolverr_startup_poll_seconds=int(
                runtime_env.get("WORKER_FLARESOLVERR_STARTUP_POLL_SECONDS", "3")
            ),
            flaresolverr_count=int(
                runtime_env.get("WORKER_FLARESOLVERR_COUNT", "4")
            ),
            flaresolverr_host=runtime_env.get("FLARESOLVERR_HOST", "localhost"),
            flaresolverr_base_port=int(
                runtime_env.get("FLARESOLVERR_BASE_PORT", "8191")
            ),
            flaresolverr_discovery_mode=runtime_env.get(
                "FLARESOLVERR_DISCOVERY_MODE",
                "env",
            ).strip().lower()
            or "env",
            flaresolverr_docker_label=runtime_env.get(
                "FLARESOLVERR_DOCKER_LABEL",
                "app=flaresolverr",
            ),
            flaresolverr_docker_image=runtime_env.get(
                "FLARESOLVERR_DOCKER_IMAGE",
                "ghcr.io/flaresolverr/flaresolverr:latest",
            ),
            flaresolverr_container_prefix=runtime_env.get(
                "FLARESOLVERR_CONTAINER_PREFIX",
                "flaresolverr-worker",
            ),
            autostart_flaresolverr=parse_bool(
                runtime_env.get("WORKER_AUTOSTART_FLARESOLVERR"),
                default=False,
            ),
            rabbitmq=RabbitMQSettings.from_env(runtime_env),
        )


@dataclass(frozen=True)
class AvailabilityCheckerSettings:
    master_url: str
    master_api_key: str
    source: str
    poll_interval_seconds: float
    request_timeout_seconds: int
    proxy_validation_concurrency: int
    output_dir: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> AvailabilityCheckerSettings:
        runtime_env = _env(env)
        return cls(
            master_url=build_master_base_url(runtime_env),
            master_api_key=runtime_env.get("MASTER_API_KEY", "").strip(),
            source=runtime_env.get(
                "AVAILABILITY_CHECKER_SOURCE",
                "master-availability-checker",
            ).strip()
            or "master-availability-checker",
            poll_interval_seconds=float(
                runtime_env.get("AVAILABILITY_CHECKER_POLL_INTERVAL_SECONDS", "60")
            ),
            request_timeout_seconds=int(
                runtime_env.get("AVAILABILITY_CHECKER_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            proxy_validation_concurrency=int(
                runtime_env.get(
                    "AVAILABILITY_CHECKER_PROXY_VALIDATION_CONCURRENCY",
                    "10",
                )
            ),
            output_dir=Path(
                runtime_env.get(
                    "AVAILABILITY_CHECKER_OUTPUT_DIR",
                    "availability_runs",
                ).strip()
                or "availability_runs"
            )
            .expanduser()
            .resolve(),
        )
