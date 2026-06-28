"""
FlareSolverr Session-Based Bot
===============================

Completes the entire Notre-Dame ticket reservation flow *inside* a single
FlareSolverr browser session.  This avoids the cookie-transfer / IP-mismatch
problems that plague the Playwright+cookie-injection approach in ``main_.py``.

Flow
----
1. Create a FlareSolverr session (persistent headless browser).
2. ``request.get`` the reservation URL → FlareSolverr solves the Cloudflare
   challenge automatically within its own browser.
3. Walk through each booking step by parsing the HTML, building the correct
   form POST payload, and submitting through the *same* session.
4. For the final Turnstile captcha on the review page, solve via CapSolver
   and inject the token.
5. Download the ticket PDF through the session.
6. Destroy the session.

Because everything happens inside a single FlareSolverr browser the cookies,
User-Agent, and exit-IP are *always* consistent — no transfer needed.

Usage (standalone)::

    python flare_bot.py

Usage (from start_flare.py or programmatically)::

    from flare_bot import run_instance, main
    await run_instance(user_details, instance_id=0)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import logging
import multiprocessing
import os
import platform
import random
import re
import requests
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from zoneinfo import ZoneInfo

import aiohttp
import dotenv
from bs4 import BeautifulSoup
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

from flare import (
    DEFAULT_MAX_TIMEOUT,
    FlareSolution,
    FlareResponse,
    FlareCookie,
    get_flaresolverr_urls,
    _next_flaresolverr_url,
    _parse_proxy,
)
from alias_manager import claim_burner_emails, create_alias, VALID_PROVIDERS
from shared.config import split_csv_urls
from util import UserDetails, get_fake_details

dotenv.load_dotenv()
disable_warnings(InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESERVATION_URL = (
    "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets"
)
BASE_URL = "https://resa.notredamedeparis.fr"

# Timeslot AJAX endpoint
TIMESLOTS_URL = f"{BASE_URL}/script/timeslots"

# Timeslot retry settings (HAR shows multiple retries due to CF 403s)
TIMESLOT_MAX_RETRIES = 1
TIMESLOT_RETRY_DELAY = 5  # seconds between retries
# When API returns 0 slots but site may have availability, retry this many times
TIMESLOT_EMPTY_RETRIES = 1
TIMESLOT_EMPTY_DELAY = 3  # seconds between empty-slot retries
DEFAULT_PAYMENT_TURNSTILE_SITEKEY = "0x4AAAAAAA1IAg9Oedxa-RnI"

ENABLE_SCREENSHOTS = True  # FlareSolverr can return screenshots of its browser
SCREENSHOTS_DIR = Path.cwd() / "screenshots"

CAPTURE_AVAILABLE_TICKETS = (
    os.getenv("CAPTURE_AVAILABLE_TICKETS", "false").strip().lower() == "true"
)

_claimed_time_slots: set[str] = set()


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw_value = os.getenv(name, "").strip()
    try:
        value = float(raw_value) if raw_value else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(value, minimum)
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name, "").strip()
    try:
        value = int(raw_value) if raw_value else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(value, minimum)
    return value

# FlareSolverr timeouts (milliseconds)
TIMEOUT_NAVIGATION = 180_000  # 3 min for CF challenge solve
TIMEOUT_FORM_POST = 60_000  # 1 min for form submissions
TIMEOUT_DOWNLOAD = 120_000  # 2 min for download page

# Waiting room settings
WAITING_ROOM_POLL_INTERVAL = _env_float(
    "WAITING_ROOM_POLL_INTERVAL_SECONDS",
    15,
    minimum=1,
)  # seconds between re-checks
WAITING_ROOM_MAX_WAIT = _env_float(
    "WAITING_ROOM_MAX_WAIT_SECONDS",
    600,
    minimum=1,
)  # max wait in queue
WAITING_ROOM_KEYWORDS = ["waiting room", "salle d'attente", "patienter", "forte affluence"]
CALENDAR_RETRY_ATTEMPTS = _env_int(
    "WORKER_CALENDAR_RETRY_ATTEMPTS",
    3,
    minimum=1,
)

COUNTRY_DIALING_PREFIXES = {
    "US": "+1",
    "USA": "+1",
    "CA": "+1",
    "GB": "+44",
    "UK": "+44",
    "FR": "+33",
}

# Staggered launch settings
INSTANCE_STAGGER_DELAY = 2  # seconds between launching each instance

# Navigation recovery settings for FlareSolverr/Cloudflare blocks
NAVIGATION_SESSION_RECOVERY_LIMIT = 4

# ---------------------------------------------------------------------------
# Logging (mirrors main_.py)
# ---------------------------------------------------------------------------

LOGS_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging() -> logging.Logger:
    """Set up logging with console + timestamped file handler."""
    logger = logging.getLogger("flare_bot")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOGS_DIR / f"flare_bot_{timestamp}.log"

    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.info(f"Log file: {log_file}")
    return logger


logger = setup_logging()

# ---------------------------------------------------------------------------
# Proxy loading (same logic as main_.py)
# ---------------------------------------------------------------------------

def _resolve_proxies_file() -> Path:
    configured_path = os.environ.get("PROXIES_FILE", "").strip()
    if not configured_path:
        return Path(__file__).resolve().parent / "proxies.txt"

    proxies_path = Path(configured_path).expanduser()
    if proxies_path.is_absolute():
        return proxies_path
    return Path(__file__).resolve().parent / proxies_path


PROXIES_FILE = _resolve_proxies_file()


def load_proxies_from_file(filepath: Path = PROXIES_FILE) -> list[str]:
    """Load proxies from a text file (one ``host:port:user:pass`` per line)."""
    if not filepath.exists():
        logger.error("Proxies file not found: %s", filepath)
        return []

    proxies: list[str] = []
    with open(filepath, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)

    logger.info("Loaded %d proxies from %s", len(proxies), filepath)
    return proxies


async def check_proxy(proxy_str: str, timeout: float = 15.0) -> bool:
    """Validate that a proxy is reachable via a public IP-echo service."""
    parts = proxy_str.split(":")
    if len(parts) < 4:
        logger.warning("Invalid proxy format for check: %s", proxy_str)
        return False

    host, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
    proxy_url = f"http://{user}:{password}@{host}:{port}"

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(
                "https://api.ipify.org?format=json",
                proxy=proxy_url,
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    logger.info(
                        "Proxy OK %s:%s -> IP %s",
                        host,
                        port,
                        data.get("ip", "?"),
                    )
                    return True
                logger.warning("Proxy %s:%s returned status %d", host, port, resp.status)
                return False
    except Exception as exc:
        logger.warning("Proxy FAIL %s:%s – %s", host, port, exc)
        return False


async def get_validated_proxies(
    needed: int,
    all_proxies: list[str],
    concurrency: int = 10,
) -> list[str]:
    """Validate and return *needed* working proxies from the pool."""
    validated: list[str] = []
    remaining = list(all_proxies)
    random.shuffle(remaining)

    while len(validated) < needed and remaining:
        batch = remaining[:concurrency]
        remaining = remaining[concurrency:]

        logger.info(
            "Checking batch of %d proxies (%d validated so far, %d needed)...",
            len(batch),
            len(validated),
            needed,
        )

        results = await asyncio.gather(
            *(check_proxy(p) for p in batch),
            return_exceptions=True,
        )

        for proxy, result in zip(batch, results):
            if result is True:
                validated.append(proxy)
                if len(validated) >= needed:
                    break

    if len(validated) < needed:
        raise RuntimeError(
            f"Only {len(validated)} working proxies found, but {needed} are required. "
            f"Add more proxies to {PROXIES_FILE}."
        )

    logger.info(
        "Proxy validation complete: %d/%d passed", len(validated), len(all_proxies)
    )
    return validated


def rotate_proxy_session(proxy_str: str) -> str:
    """Rotate sticky session token in ``host:port:user:pass`` proxy string.

    iproyal proxies keep the session in the *password* field, e.g.
    ``sa68A7xvj0LaL6b6_country-us_session-XXX_lifetime-24h``.
    Other providers may embed it in the username.  We check both.
    """
    parts = proxy_str.split(":")
    if len(parts) < 4:
        return proxy_str

    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    token = "".join(
        random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        for _ in range(8)
    )

    new_user = user
    new_password = password

    # Rotate session in whichever field actually contains it
    if "_session-" in password:
        new_password = re.sub(r"_session-[^_]+", f"_session-{token}", password, count=1)
    elif "session-" in password:
        new_password = re.sub(r"session-[^_]+", f"session-{token}", password, count=1)
    elif "_session-" in user:
        new_user = re.sub(r"_session-[^_]+", f"_session-{token}", user, count=1)
    elif "session-" in user:
        new_user = re.sub(r"session-[^_]+", f"session-{token}", user, count=1)
    else:
        # No existing session token found — add to password as that's the
        # common pattern for iproyal residential proxies
        new_password = f"{password}_session-{token}"

    return f"{host}:{port}:{new_user}:{new_password}"


def is_cf_block_message(message: str) -> bool:
    """Detect known Cloudflare-block wording from FlareSolverr errors."""
    msg = message.lower()
    return (
        "cloudflare has blocked this request" in msg
        or "error solving the challenge" in msg
    )


UPSTREAM_PROXIES: list[str] = load_proxies_from_file()
UPSTREAM_PROXY = UPSTREAM_PROXIES[0] if UPSTREAM_PROXIES else ""



# ---------------------------------------------------------------------------
# Instance stage tracking (shared across processes via Manager dict)
# ---------------------------------------------------------------------------


def _update_stage(
    instance_id: int,
    stage: str,
    status_dict: dict | None = None,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    payload = {
        "instance_id": instance_id,
        "stage": stage,
        "outcome": "running",
        "error": "",
        "ts": datetime.now().strftime("%H:%M:%S"),
    }
    if status_dict is not None:
        status_dict[instance_id] = payload
    if status_callback is not None:
        try:
            status_callback(dict(payload))
        except Exception:
            logger.debug("Status callback failed during stage update", exc_info=True)


def _finish_stage(
    instance_id: int,
    outcome: str,
    status_dict: dict | None = None,
    error: str = "",
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    stage: str = "",
) -> None:
    prev = status_dict.get(instance_id, {}) if status_dict is not None else {}
    payload = {
        "instance_id": instance_id,
        "stage": stage or prev.get("stage", "unknown"),
        "outcome": outcome,
        "error": error[:120],
        "ts": datetime.now().strftime("%H:%M:%S"),
    }
    if status_dict is not None:
        status_dict[instance_id] = payload
    if status_callback is not None:
        try:
            status_callback(dict(payload))
        except Exception:
            logger.debug("Status callback failed during finish update", exc_info=True)


def print_run_summary(
    status_dict: dict,
    num_instances: int,
    logger_obj: logging.Logger,
) -> None:
    divider = "═" * 72
    lines: list[str] = ["", divider, "  RUN SUMMARY", divider]

    success_count = error_count = interrupted_count = running_count = 0

    for i in range(num_instances):
        info = status_dict.get(i)
        if info is None:
            lines.append(f"  Instance {i:>2}  │  ⬜  (no data)")
            continue

        stage = info.get("stage", "unknown")
        outcome = info.get("outcome", "unknown")
        error = info.get("error", "")
        ts = info.get("ts", "")

        if outcome == "success":
            icon = "✅"
            success_count += 1
        elif outcome == "error":
            icon = "❌"
            error_count += 1
        elif outcome == "interrupted":
            icon = "⚠️"
            interrupted_count += 1
        else:
            icon = "🔄"
            running_count += 1

        line = f"  Instance {i:>2}  │  {icon}  {stage}"
        if error:
            line += f"  —  {error}"
        if ts:
            line += f"  [{ts}]"
        lines.append(line)

    lines.append(divider)
    lines.append(
        f"  Totals:  ✅ {success_count} succeeded  │  "
        f"❌ {error_count} failed  │  "
        f"⚠️ {interrupted_count} interrupted  │  "
        f"🔄 {running_count} still running"
    )
    lines.append(divider)

    summary_text = "\n".join(lines)
    print(summary_text)
    for ln in lines:
        logger_obj.info(ln)


# ---------------------------------------------------------------------------
# Validation (same as main_.py)
# ---------------------------------------------------------------------------


def validate_user_details(user_details: UserDetails) -> None:
    """Validate user details before processing."""
    if not user_details:
        raise ValueError("User details cannot be None")

    required_string_fields = [
        "unique_id",
        "date",
        "firstName",
        "lastName",
        "email",
        "phone",
        "zip",
        "country",
        "time",
        "job_time",
        "status",
    ]

    for field_name in required_string_fields:
        value = getattr(user_details, field_name, None)
        if not value or not isinstance(value, str) or not value.strip():
            raise ValueError(f"Required string field '{field_name}' is missing or empty")

    if not isinstance(user_details.ticket_count, int) or user_details.ticket_count <= 0:
        raise ValueError("Field 'ticket_count' must be a positive integer")

    email = user_details.email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError(f"Invalid email format: {email}")

    phone = user_details.phone.strip()
    if not phone.replace("-", "").replace(" ", "").isdigit() or len(phone) < 10:
        raise ValueError(f"Invalid phone number format: {phone}")

    time_parts = user_details.time.split(":")
    if len(time_parts) != 2 or not all(part.isdigit() for part in time_parts):
        raise ValueError(f"Invalid time format: {user_details.time}. Expected HH:MM")

    hour, minute = map(int, time_parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time values: {user_details.time}")

    job_time_parts = user_details.job_time.split(":")
    if len(job_time_parts) != 2 or not all(part.isdigit() for part in job_time_parts):
        raise ValueError(
            f"Invalid job_time format: {user_details.job_time}. Expected HH:MM"
        )

    job_hour, job_minute = map(int, job_time_parts)
    if not (0 <= job_hour <= 23 and 0 <= job_minute <= 59):
        raise ValueError(f"Invalid job_time values: {user_details.job_time}")


# ---------------------------------------------------------------------------
# Date helper (mirrors main_.py)
# ---------------------------------------------------------------------------


def day_with_name(date_input: str) -> str:
    """Return e.g. ``'Monday 5'`` for a date string."""
    if "-" in date_input and len(date_input.split("-")) == 3:
        year, month, day = map(int, date_input.split("-"))
        date_obj = datetime(year, month, day)
    else:
        today = datetime.today()
        date_obj = datetime(today.year, today.month, int(date_input))

    if platform.system() == "Windows":
        return date_obj.strftime("%A %#d")
    else:
        return date_obj.strftime("%A %-d")


def _sanitize_artifact_part(value: object, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())
    text = text.strip("-_")
    return text or fallback


def _resolve_artifact_date(date_value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value.strip()):
        return date_value.strip()
    return datetime.now().strftime("%Y-%m-%d")


def build_artifact_name(
    step_name: str,
    user_details: UserDetails,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    metadata = run_metadata or {}
    worker_name = metadata.get("worker_name") or metadata.get("worker_id") or "standalone"
    try_number_value = metadata.get("try_number", metadata.get("retry_count", 0))
    try:
        try_number = int(try_number_value)
    except (TypeError, ValueError):
        try_number = 0
    if try_number <= 0:
        try_number = 1

    return "_".join(
        [
            _sanitize_artifact_part(step_name, "step"),
            _sanitize_artifact_part(worker_name, "worker"),
            _resolve_artifact_date(user_details.date),
            f"try{try_number}",
        ]
    )


# ---------------------------------------------------------------------------
# FlareSolverr Session Client
# ---------------------------------------------------------------------------


class FlareSession:
    """
    Wrapper around the FlareSolverr session API.

    Manages a persistent browser session that keeps cookies, storage, and
    User-Agent consistent across all requests.
    """

    class RequestError(Exception):
        """Raised when FlareSolverr returns HTTP >= 400."""

        def __init__(
            self,
            status_code: int,
            body: str,
            url: str,
        ) -> None:
            self.status_code = status_code
            self.body = body
            self.url = url
            super().__init__(f"FlareSolverr HTTP {status_code}: {body[:200]}")

        @property
        def is_cloudflare_block(self) -> bool:
            body_lower = self.body.lower()
            return (
                "cloudflare has blocked this request" in body_lower
                or "error solving the challenge" in body_lower
            )

    def __init__(
        self,
        flaresolverr_url: str | None = None,
        upstream_proxy: str = "",
        max_timeout: int = DEFAULT_MAX_TIMEOUT,
        instance_id: int = 0,
    ):
        self.flaresolverr_url = flaresolverr_url or _next_flaresolverr_url()
        self.upstream_proxy = upstream_proxy
        self.max_timeout = max_timeout
        self.instance_id = instance_id
        self.session_id: str | None = None
        self._http: aiohttp.ClientSession | None = None

        # Last response data
        self.last_url: str = ""
        self.last_status: int = 0
        self.last_html: str = ""
        self.last_cookies: list[FlareCookie] = []
        self.last_user_agent: str = ""
        self.last_screenshot_base64: str = ""

    def _log(self, level: int, msg: str) -> None:
        logger.log(level, f"[Instance {self.instance_id}] [FlareSession] {msg}")

    def _merge_request_headers(
        self,
        headers: Dict[str, str] | None = None,
        *,
        sync_cookies: bool = False,
    ) -> Dict[str, str]:
        merged_headers = dict(headers or {})
        if sync_cookies and self.last_cookies:
            cookie_header = "; ".join(
                f"{cookie.name}={cookie.value}"
                for cookie in self.last_cookies
                if getattr(cookie, "name", "") and getattr(cookie, "value", "") is not None
            )
            if cookie_header:
                merged_headers.setdefault("Cookie", cookie_header)
                self._log(
                    logging.DEBUG,
                    f"Syncing {len(self.last_cookies)} cookie(s) into browser request",
                )
        return merged_headers

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            timeout = aiohttp.ClientTimeout(total=self.max_timeout / 1000 + 60)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def _request(self, payload: dict) -> dict:
        """Send a request to the FlareSolverr API and return the JSON body."""
        http = await self._get_http()
        headers = {"Content-Type": "application/json"}
        self._log(logging.DEBUG, f"FlareSolverr request: cmd={payload.get('cmd')}")

        async with http.post(
            self.flaresolverr_url,
            headers=headers,
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                self._log(
                    logging.ERROR,
                    f"FlareSolverr HTTP {resp.status}: {body[:500]}",
                )
                raise FlareSession.RequestError(
                    status_code=resp.status,
                    body=body,
                    url=str(resp.url),
                )
            data = await resp.json(content_type=None)

        status = data.get("status", "")
        message = data.get("message", "")
        self._log(logging.DEBUG, f"FlareSolverr response: status={status}, msg={message}")

        # Update last response metadata
        solution = data.get("solution", {})
        self.last_url = solution.get("url", self.last_url)
        self.last_status = solution.get("status", self.last_status)
        self.last_html = solution.get("response", "")
        self.last_user_agent = solution.get("userAgent", self.last_user_agent)
        self.last_screenshot_base64 = str(data.get("screenshot", "") or "")
        self.last_cookies = [
            FlareCookie.from_dict(c) for c in solution.get("cookies", [])
        ]

        if status != "ok":
            self._log(logging.WARNING, f"FlareSolverr non-ok: {message}")

        return data

    # ── Session lifecycle ─────────────────────────────────────────────

    async def create(self) -> str:
        """Create a new FlareSolverr browser session."""
        payload: Dict[str, Any] = {"cmd": "sessions.create"}
        if self.upstream_proxy:
            payload["proxy"] = _parse_proxy(self.upstream_proxy)

        data = await self._request(payload)
        self.session_id = data.get("session", "") or ""
        self._log(logging.INFO, f"Session created: {self.session_id}")
        return self.session_id

    async def destroy(self) -> None:
        """Destroy the current FlareSolverr session."""
        if not self.session_id:
            return
        try:
            await self._request(
                {"cmd": "sessions.destroy", "session": self.session_id}
            )
            self._log(logging.INFO, f"Session destroyed: {self.session_id}")
        except Exception as e:
            self._log(logging.WARNING, f"Error destroying session: {e}")
        finally:
            self.session_id = None

    async def close(self) -> None:
        """Destroy session and close HTTP client."""
        await self.destroy()
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Navigation ────────────────────────────────────────────────────

    async def get(
        self,
        url: str,
        timeout: int | None = None,
        return_screenshot: bool = False,
        headers: Dict[str, str] | None = None,
        sync_cookies: bool = False,
    ) -> FlareResponse:
        """Navigate via GET inside the persistent session."""
        if not self.session_id:
            raise RuntimeError("Session not created. Call create() first.")

        merged_headers = self._merge_request_headers(
            headers,
            sync_cookies=sync_cookies,
        )
        payload: Dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "session": self.session_id,
            "maxTimeout": timeout or self.max_timeout,
            "returnScreenshot": return_screenshot,
        }
        if merged_headers:
            payload["headers"] = merged_headers
        if sync_cookies and self.last_cookies:
            payload["cookies"] = [c.to_dict(url=url) for c in self.last_cookies]

        self._log(logging.INFO, f"GET {url}")
        if merged_headers:
            self._log(logging.DEBUG, f"GET headers: {list(merged_headers.keys())}")
        data = await self._request(payload)
        return FlareResponse.from_dict(data)

    async def post(
        self,
        url: str,
        post_data: str = "",
        timeout: int | None = None,
        return_screenshot: bool = False,
        headers: Dict[str, str] | None = None,
        sync_cookies: bool = False,
    ) -> FlareResponse:
        """Submit a POST request inside the persistent session."""
        if not self.session_id:
            raise RuntimeError("Session not created. Call create() first.")

        merged_headers = self._merge_request_headers(
            headers,
            sync_cookies=sync_cookies,
        )

        payload: Dict[str, Any] = {
            "cmd": "request.post",
            "url": url,
            "session": self.session_id,
            "maxTimeout": timeout or self.max_timeout,
            "postData": post_data,
            "returnScreenshot": return_screenshot,
        }
        if merged_headers:
            payload["headers"] = merged_headers
        if sync_cookies and self.last_cookies:
            payload["cookies"] = [c.to_dict(url=url) for c in self.last_cookies]

        self._log(logging.INFO, f"POST {url}")
        if merged_headers:
            self._log(logging.DEBUG, f"POST headers: {list(merged_headers.keys())}")
        data = await self._request(payload)
        return FlareResponse.from_dict(data)

    def _build_direct_http_session(self) -> requests.Session:
        proxy_url: str | None = None
        if self.upstream_proxy:
            parts = self.upstream_proxy.split(":")
            if len(parts) >= 4:
                host, port, user = parts[0], parts[1], parts[2]
                password = ":".join(parts[3:])
                proxy_url = f"http://{user}:{password}@{host}:{port}"

        use_cffi = False
        try:
            from curl_cffi import requests as cffi_requests
            # curl_cffi requires proxies + impersonate in the constructor; setting
            # them after creation via .proxies.update() is ignored by the C backend.
            cffi_proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
            http = cffi_requests.Session(impersonate="chrome124", proxies=cffi_proxies)
            use_cffi = True
        except ImportError:
            http = requests.Session()
        # Only set UA for plain requests.Session — curl_cffi must use its own
        # Chrome 124 UA so TLS fingerprint and UA string stay consistent.
        if not use_cffi and self.last_user_agent:
            http.headers.update({"User-Agent": self.last_user_agent})

        if proxy_url and not use_cffi:
            http.proxies.update({"http": proxy_url, "https": proxy_url})

        for cookie in self.last_cookies:
            cookie_kwargs: dict[str, str] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
            }
            if cookie.domain:
                cookie_kwargs["domain"] = cookie.domain
            http.cookies.set(**cookie_kwargs)

        return http

    @staticmethod
    def _flare_cookies_from_requests_jar(
        cookie_jar,
    ) -> list[FlareCookie]:
        cookies: list[FlareCookie] = []
        for cookie in cookie_jar:
            if isinstance(cookie, str):
                # curl_cffi Cookies jar iterates over names (keys)
                cookies.append(
                    FlareCookie(
                        name=cookie,
                        value=str(cookie_jar[cookie]),
                        domain="",
                        path="/",
                        expiry=None,
                        http_only=False,
                        secure=False,
                        same_site="Lax",
                    )
                )
            else:
                # requests.Session cookie objects
                rest = getattr(cookie, "_rest", {}) or {}
                same_site = str(
                    rest.get("SameSite") or rest.get("samesite") or "Lax"
                )
                cookies.append(
                    FlareCookie(
                        name=str(cookie.name),
                        value=str(cookie.value),
                        domain=str(cookie.domain or ""),
                        path=str(cookie.path or "/"),
                        expiry=cookie.expires,
                        http_only=("HttpOnly" in rest) or bool(rest.get("HttpOnly")),
                        secure=bool(cookie.secure),
                        same_site=same_site,
                    )
                )
        return cookies

    async def post_direct_form(
        self,
        url: str,
        post_data: str = "",
        timeout: int | None = None,
        headers: Dict[str, str] | None = None,
    ) -> FlareResponse:
        """Submit a form via a direct HTTP session that reuses solved cookies."""

        def _run() -> dict[str, Any]:
            http = self._build_direct_http_session()
            merged_headers = dict(headers or {})
            user_agent = str(http.headers.get("User-Agent", "") or "").strip()
            if user_agent:
                merged_headers.setdefault("User-Agent", user_agent)

            try:
                response = http.post(
                    url,
                    data=post_data,
                    headers=merged_headers,
                    timeout=(timeout or self.max_timeout) / 1000 + 60,
                    verify=False,
                )
                body = response.text
                return {
                    "url": str(response.url),
                    "status": int(response.status_code),
                    "body": body,
                    "headers": dict(response.headers),
                    "cookies": self._flare_cookies_from_requests_jar(http.cookies),
                    "user_agent": user_agent,
                }
            finally:
                http.close()

        self._log(logging.INFO, f"DIRECT POST {url}")
        if headers:
            self._log(logging.DEBUG, f"DIRECT POST headers: {list(headers.keys())}")

        result = await asyncio.to_thread(_run)
        self.last_url = str(result["url"])
        self.last_status = int(result["status"])
        self.last_html = str(result["body"])
        self.last_cookies = list(result["cookies"])
        self.last_user_agent = str(result["user_agent"] or self.last_user_agent)
        self.last_screenshot_base64 = ""

        return FlareResponse(
            status="ok",
            message="Direct HTTP form submission completed",
            solution=FlareSolution(
                url=self.last_url,
                status=self.last_status,
                cookies=self.last_cookies,
                user_agent=self.last_user_agent,
                headers=dict(result["headers"]),
                response=self.last_html,
            ),
            version="direct-http",
        )

    async def save_screenshot(self, name: str) -> None:
        """Take a screenshot via FlareSolverr and save to disk."""
        if not ENABLE_SCREENSHOTS or not self.session_id:
            return
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            if not self.last_url:
                self._log(logging.WARNING, "Cannot capture screenshot without a current URL")
                return
            # Navigate the current page with screenshot enabled
            await self.get(self.last_url, return_screenshot=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(
                c for c in name if c.isalnum() or c in ("-", "_")
            ).strip()
            base_name = f"flare_{self.instance_id}_{timestamp}_{safe_name}"

            if self.last_screenshot_base64:
                png_path = SCREENSHOTS_DIR / f"{base_name}.png"
                png_path.write_bytes(base64.b64decode(self.last_screenshot_base64))
                self._log(logging.DEBUG, f"Screenshot saved: {png_path}")
            else:
                self._log(logging.WARNING, "FlareSolverr returned no screenshot payload")

            html_path = SCREENSHOTS_DIR / f"{base_name}.html"
            html_path.write_text(self.last_html[:500_000], encoding="utf-8")
            self._log(logging.DEBUG, f"HTML snapshot saved: {html_path}")
        except Exception as e:
            self._log(logging.WARNING, f"Screenshot/snapshot failed: {e}")


# ---------------------------------------------------------------------------
# HTML Parsing Helpers
# ---------------------------------------------------------------------------


def parse_html(html: str) -> BeautifulSoup:
    """Parse HTML string into a BeautifulSoup tree."""
    return BeautifulSoup(html, "html.parser")


def extract_form(soup: BeautifulSoup, selector: str = "form") -> dict:
    """
    Extract form action URL and all hidden/default field values.

    Returns:
        ``{"action": str, "method": str, "fields": {name: value}}``
    """
    form = soup.select_one(selector)
    if form is None:
        return {"action": "", "method": "post", "fields": {}}

    action = str(form.get("action", "") or "")
    method = str(form.get("method", "post") or "post").lower()

    fields: Dict[str, str] = {}
    for inp in form.select("input[name]"):
        name = str(inp.get("name", "") or "")
        value = str(inp.get("value", "") or "")
        # For checkboxes/radios only include if checked
        input_type = str(inp.get("type", "") or "").lower()
        if input_type in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                fields[name] = value or "on"
        else:
            fields[name] = value

    for sel in form.select("select[name]"):
        name = str(sel.get("name", "") or "")
        selected_opt = sel.select_one("option[selected]")
        if selected_opt:
            fields[name] = str(selected_opt.get("value", "") or "")
        else:
            # Use first non-empty option as default
            for opt in sel.select("option"):
                val = str(opt.get("value", "") or "")
                if val:
                    fields[name] = val
                    break

    for ta in form.select("textarea[name]"):
        name = str(ta.get("name", "") or "")
        fields[name] = ta.get_text() or ""

    return {"action": str(action), "method": method, "fields": fields}


def resolve_url(base: str, action: str) -> str:
    """Resolve a potentially relative form action against a base URL."""
    if not action:
        return base
    if action.startswith("http"):
        return action
    if action.startswith("/"):
        # Absolute path
        from urllib.parse import urlparse

        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{action}"
    # Relative path
    if "?" in base:
        base = base.rsplit("?", 1)[0]
    base_dir = base.rsplit("/", 1)[0]
    return f"{base_dir}/{action}"


def extract_csrf_token(soup: BeautifulSoup) -> str:
    """Extract CSRF / anti-forgery token from common locations."""
    # Meta tag
    meta = soup.select_one('meta[name="csrf-token"]')
    if meta:
        return str(meta.get("content", "") or "")

    # Hidden input
    for name_attr in ("_token", "csrf_token", "_csrf", "csrfmiddlewaretoken"):
        inp = soup.select_one(f'input[name="{name_attr}"]')
        if inp:
            return str(inp.get("value", "") or "")

    return ""


def build_navigation_post_headers(referer_url: str) -> dict[str, str]:
    """Build same-origin headers for HTML form submissions."""
    referer = referer_url or RESERVATION_URL
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": referer,
    }


def build_ajax_post_headers(referer_url: str) -> dict[str, str]:
    """Build same-origin headers for AJAX form submissions."""
    referer = referer_url or f"{BASE_URL}/en/reservationindividuelle/date"
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }


def _normalize_international_phone_number(
    phone: str,
    *,
    country_code: str = "",
    country_name: str = "",
) -> str:
    text = str(phone or "").strip()
    if not text:
        return ""

    digits = re.sub(r"\D+", "", text)
    if not digits:
        return ""

    if text.startswith("+"):
        return f"+{digits}"

    normalized_country_code = country_code.strip().upper()
    normalized_country_name = country_name.strip().lower()
    dialing_prefix = COUNTRY_DIALING_PREFIXES.get(normalized_country_code, "")

    if not dialing_prefix:
        if normalized_country_name in {
            "united states of america",
            "united states",
            "usa",
            "canada",
        }:
            dialing_prefix = "+1"
        elif normalized_country_name in {
            "united kingdom",
            "great britain",
            "england",
        }:
            dialing_prefix = "+44"
        elif normalized_country_name in {"france"}:
            dialing_prefix = "+33"

    if dialing_prefix:
        prefix_digits = dialing_prefix.lstrip("+")
        if digits.startswith(prefix_digits):
            return f"+{digits}"
        return f"{dialing_prefix}{digits}"

    return f"+{digits}"


# ---------------------------------------------------------------------------
# Page Object Model — Session-based (HTTP only)
# ---------------------------------------------------------------------------


class BasePageSession:
    """Base class for all page objects in the session-based flow."""

    def __init__(self, session: FlareSession, instance_id: int = 0):
        self.session = session
        self.instance_id = instance_id
        self.page_name = self.__class__.__name__

    def _log(self, level: int, message: str) -> None:
        logger.log(
            level, f"[Instance {self.instance_id}] [{self.page_name}] {message}"
        )

    @property
    def soup(self) -> BeautifulSoup:
        """Parse the last HTML response."""
        return parse_html(self.session.last_html)

    async def save_snapshot(self, name: str) -> None:
        """Save an HTML snapshot of the current page state."""
        if not ENABLE_SCREENSHOTS:
            return
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(
                c for c in name if c.isalnum() or c in ("-", "_")
            ).strip()
            fname = (
                SCREENSHOTS_DIR
                / f"flare_{self.instance_id}_{timestamp}_{safe_name}.html"
            )
            html = self.session.last_html
            fname.write_text(html[:500_000], encoding="utf-8")
            self._log(logging.DEBUG, f"Snapshot saved: {fname}")
        except Exception as e:
            self._log(logging.WARNING, f"Failed to save snapshot: {e}")

    def _extract_form(self, selector: str = "form") -> dict:
        """Extract form details from the current page."""
        return extract_form(self.soup, selector)

    def _extract_csrf_pair(
        self,
        *,
        context_label: str,
    ) -> tuple[str, str]:
        soup = self.soup
        csrf_name = str(
            (soup.select_one('input[name="csrf_name"]') or {}).get("value", "")  # type: ignore[union-attr]
        )
        csrf_value = str(
            (soup.select_one('input[name="csrf_value"]') or {}).get("value", "")  # type: ignore[union-attr]
        )
        self._record_csrf_fetch(
            context_label=context_label,
            csrf_name=csrf_name,
            csrf_value=csrf_value,
        )
        return csrf_name, csrf_value

    def _record_csrf_fetch(
        self,
        *,
        context_label: str,
        csrf_name: str,
        csrf_value: str,
    ) -> None:
        url = self.session.last_url or ""
        record = {
            "page": self.page_name,
            "context": context_label,
            "url": url,
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "fingerprint": self._csrf_fingerprint(csrf_name, csrf_value),
        }
        setattr(self.session, "_latest_csrf_record", record)
        if csrf_name and csrf_value:
            self._log(
                logging.INFO,
                "Fetched CSRF pair "
                f"[{context_label}] fingerprint={record['fingerprint']} "
                f"url={url or '<unknown>'} "
                f"csrf_name={self._mask_token(csrf_name)} "
                f"csrf_value={self._mask_token(csrf_value)}",
            )
        else:
            self._log(
                logging.WARNING,
                "Fetched CSRF pair "
                f"[{context_label}] is incomplete "
                f"url={url or '<unknown>'} "
                f"csrf_name={self._mask_token(csrf_name)} "
                f"csrf_value={self._mask_token(csrf_value)}",
            )

    def _log_csrf_usage(
        self,
        *,
        target_url: str,
        fields: dict[str, str],
        usage_label: str,
    ) -> None:
        csrf_name = str(fields.get("csrf_name", "") or "")
        csrf_value = str(fields.get("csrf_value", "") or "")
        latest = getattr(self.session, "_latest_csrf_record", None)

        if not csrf_name or not csrf_value:
            self._log(
                logging.INFO,
                f"CSRF usage [{usage_label}] -> {target_url}: "
                "request does not include csrf_name/csrf_value fields",
            )
            return

        request_fingerprint = self._csrf_fingerprint(csrf_name, csrf_value)
        if not isinstance(latest, dict):
            self._log(
                logging.WARNING,
                f"CSRF usage [{usage_label}] -> {target_url}: "
                f"no latest fetched CSRF record is available; "
                f"request fingerprint={request_fingerprint}",
            )
            return

        latest_name = str(latest.get("csrf_name", "") or "")
        latest_value = str(latest.get("csrf_value", "") or "")
        latest_fingerprint = str(latest.get("fingerprint", "") or "")
        latest_context = str(latest.get("context", "") or "")
        latest_url = str(latest.get("url", "") or "")

        if csrf_name == latest_name and csrf_value == latest_value:
            self._log(
                logging.INFO,
                f"CSRF usage [{usage_label}] -> {target_url}: "
                f"using latest fetched pair fingerprint={request_fingerprint} "
                f"from [{latest_context}] url={latest_url or '<unknown>'}",
            )
            return

        self._log(
            logging.WARNING,
            f"CSRF usage [{usage_label}] -> {target_url}: "
            f"request fingerprint={request_fingerprint} does NOT match latest "
            f"fetched fingerprint={latest_fingerprint} from "
            f"[{latest_context}] url={latest_url or '<unknown>'}",
        )

    @staticmethod
    def _csrf_fingerprint(csrf_name: str, csrf_value: str) -> str:
        if not csrf_name and not csrf_value:
            return "missing"
        digest = hashlib.sha1(f"{csrf_name}|{csrf_value}".encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _mask_token(value: str) -> str:
        text = str(value or "")
        if not text:
            return "<missing>"
        if len(text) <= 10:
            return text
        return f"{text[:6]}...{text[-4:]}"

    def _raise_if_invalid_csrf_response(self, *, step_label: str) -> None:
        body = self.session.last_html or ""
        if "Invalid CSRF token provided" not in body:
            return
        current_url = self.session.last_url or "<unknown>"
        self._log(
            logging.ERROR,
            f"{step_label} was rejected by the site with 'Invalid CSRF token provided' "
            f"(url={current_url})",
        )
        raise Exception(f"{step_label} rejected by site: invalid CSRF token")

    def _raise_if_order_limit_reached(self, *, step_label: str) -> None:
        soup = self.soup
        container = soup.select_one(".error-container.orderLimitReached")
        if container is None:
            body = self.session.last_html or ""
            if "Maximum amount of orders has been reached" not in body:
                return
            message = "Maximum amount of orders has been reached."
        else:
            message = " ".join(container.stripped_strings).strip()
            if not message:
                message = "Maximum amount of orders has been reached."

        current_url = self.session.last_url or "<unknown>"
        self._log(
            logging.ERROR,
            f"{step_label} blocked by site order limit: {message} (url={current_url})",
        )
        raise Exception(f"{step_label} blocked by site: slot capacity reached | {message}")

    async def submit_form(
        self,
        extra_fields: dict | None = None,
        form_selector: str = "form",
        override_action: str = "",
    ) -> FlareResponse:
        """
        Extract the form from the current page, merge extra fields, and POST.

        Args:
            extra_fields: Additional/override field values.
            form_selector: CSS selector for the form element.
            override_action: Force a specific action URL.

        Returns:
            The ``FlareResponse`` from the POST.
        """
        form_data = self._extract_form(form_selector)
        fields = form_data["fields"]
        if extra_fields:
            fields.update(extra_fields)

        action = override_action or form_data["action"]
        url = resolve_url(self.session.last_url, action)

        # Build URL-encoded form body
        from urllib.parse import urlencode

        post_body = urlencode(fields)

        self._log(
            logging.DEBUG,
            f"Submitting form to {url} with {len(fields)} field(s)",
        )
        self._log(logging.DEBUG, f"Form fields: {list(fields.keys())}")
        self._log_csrf_usage(
            target_url=url,
            fields=fields if isinstance(fields, dict) else dict(fields),
            usage_label="generic_form_submit",
        )
        request_headers = build_navigation_post_headers(self.session.last_url or url)
        return await self.session.post_direct_form(
            url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )


class HomePageSession(BasePageSession):
    """Step 1: Select ticket count on the /tickets page.

    HAR flow:
      GET /en/reservationindividuelle/tickets  → HTML with form containing
          csrf_name, csrf_value, token_tickets hidden inputs
          and a ``select[name="tickets[411622]"]`` dropdown.
      POST /en/reservationindividuelle/date    → navigates to calendar page.
    """

    def _is_waiting_room(self, soup: BeautifulSoup) -> bool:
        """Detect whether the current page is a waiting room / queue page."""
        page_text = soup.get_text(separator=" ", strip=True).lower()
        return any(kw in page_text for kw in WAITING_ROOM_KEYWORDS)

    async def wait_for_load(self) -> None:
        """Wait through the waiting room (if any) then verify the tickets page loaded.

        When the site is under heavy load it serves a waiting-room page
        instead of the real tickets form.  This method polls by re-GETting
        the reservation URL until the form appears or
        ``WAITING_ROOM_MAX_WAIT`` seconds elapse.
        """
        self._log(logging.INFO, "Checking tickets page loaded...")
        start = asyncio.get_event_loop().time()
        attempt = 0

        while True:
            soup = self.soup
            form = soup.select_one("form")
            csrf_ok = soup.select_one('input[name="csrf_name"]') is not None

            # Happy path — form with CSRF is present
            if form is not None and csrf_ok:
                self._log(logging.INFO, "Tickets page loaded successfully")
                await self.save_snapshot("home_page_loaded")
                return

            # Check if we're in the waiting room
            in_waiting_room = self._is_waiting_room(soup)
            elapsed = asyncio.get_event_loop().time() - start

            if in_waiting_room:
                if elapsed > WAITING_ROOM_MAX_WAIT:
                    raise Exception(
                        f"Stuck in waiting room for {elapsed:.0f}s "
                        f"(max {WAITING_ROOM_MAX_WAIT}s) — giving up"
                    )
                attempt += 1
                self._log(
                    logging.INFO,
                    f"In waiting room (attempt {attempt}, "
                    f"{elapsed:.0f}s elapsed). "
                    f"Re-checking in {WAITING_ROOM_POLL_INTERVAL}s...",
                )
                await asyncio.sleep(WAITING_ROOM_POLL_INTERVAL)
                # Re-fetch the page — the server will redirect when our
                # turn comes, or serve the form directly
                await self.session.get(
                    RESERVATION_URL, timeout=TIMEOUT_NAVIGATION
                )
                continue

            # Not a waiting room but form/CSRF still missing — unexpected
            body_text = soup.get_text(separator=" ", strip=True)[:300]
            if form is None:
                self._log(
                    logging.WARNING,
                    f"Form not found on tickets page. Page text: {body_text}",
                )
                raise Exception("Tickets page did not load — form missing")

            self._log(
                logging.WARNING,
                "csrf_name hidden input not found — page may still be on CF challenge",
            )
            raise Exception(f"Tickets page missing csrf_name. Text: {body_text}")

    async def select_tickets_and_submit(self, count: int) -> FlareResponse:
        """Extract CSRF + token_tickets, set ticket count, POST to /date.

        HAR POST body::

            csrf_name=csrf6996ac83e32a4
            &csrf_value=29c971b60db6bcf58b67479d543e79cf
            &token_tickets=6e8280bb2632ad...
            &tickets[411622]=1
            &donation-input=0
            &donationCheck=true
        """
        self._log(logging.INFO, f"Selecting {count} ticket(s) and submitting")

        soup = self.soup
        csrf_name, csrf_value = self._extract_csrf_pair(
            context_label="tickets_page_loaded"
        )
        token_tickets = str(
            (soup.select_one('input[name="token_tickets"]') or {}).get("value", "")  # type: ignore[union-attr]
        )

        self._log(logging.DEBUG, f"csrf_name={csrf_name[:20]}...")
        self._log(logging.DEBUG, f"token_tickets={token_tickets[:20]}...")

        if not csrf_name or not csrf_value:
            self._log(logging.ERROR, "CSRF tokens missing from tickets page")
            raise Exception("CSRF tokens not found on tickets page")

        # Find ticket select — name pattern is tickets[<product_id>]
        ticket_select = soup.select_one('select[name^="tickets["]')
        if ticket_select:
            ticket_field_name = str(ticket_select.get("name", "") or "")
        else:
            # Fallback to the known product ID from HAR
            ticket_field_name = "tickets[411622]"
            self._log(
                logging.WARNING,
                f"Ticket select not found, using default: {ticket_field_name}",
            )

        # Build POST payload matching HAR
        from urllib.parse import urlencode

        fields: Dict[str, str] = {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "token_tickets": token_tickets,
            ticket_field_name: str(count),
            "donation-input": "0",
            "donationCheck": "true",
        }

        post_body = urlencode(fields)
        target_url = f"{BASE_URL}/en/reservationindividuelle/date"
        request_headers = build_navigation_post_headers(
            self.session.last_url or RESERVATION_URL
        )
        self._log(logging.DEBUG, f"POSTing to {target_url} with fields: {list(fields.keys())}")
        self._log_csrf_usage(
            target_url=target_url,
            fields=fields,
            usage_label="tickets_to_date_submit",
        )
        res = await self.session.post_direct_form(
            target_url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )
        self._log(logging.INFO, f"Tickets submitted -> {self.session.last_url}")
        await self.save_snapshot("home_page_submitted")
        self._raise_if_invalid_csrf_response(step_label="Tickets submit")
        return res


class TrustedSlotSubmissionFailed(Exception):
    """Raised when a broker-provided slot submit does not reach the details page."""


class CalendarPageSession(BasePageSession):
    """Step 2: Select date and time on the calendar page.

    HAR flow:
      The calendar page is already loaded after submitting tickets.
      1. Extract csrf_name/csrf_value from the calendar page HTML.
      2. AJAX POST to ``/script/timeslots`` to fetch available slots (with retry).
      3. POST csrf + ticketDate + ticketTime to
         ``/en/reservationindividuelle/personal-details``.
    """

    _SLOT_PRIORITY = ["timeslotQuiet", "timeslotBusy", "timeslotAlmostFull"]

    def __init__(
        self,
        session: FlareSession,
        instance_id: int = 0,
        on_403_refresh: Callable[[], Awaitable[None]] | None = None,
    ):
        super().__init__(session, instance_id)
        self.on_403_refresh = on_403_refresh

    def _extract_ticket_date_window(self) -> tuple[date, date] | None:
        """Extract inclusive date range from calendar HTML JS vars."""
        html = self.session.last_html or ""
        min_match = re.search(
            r"var\s+ticketMinDate\s*=\s*new\s+Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*;",
            html,
        )
        max_match = re.search(
            r"var\s+ticketMaxDate\s*=\s*new\s+Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*;",
            html,
        )
        if not min_match or not max_match:
            return None

        try:
            min_year, min_month_zero_based, min_day = map(int, min_match.groups())
            max_year, max_month_zero_based, max_day = map(int, max_match.groups())
            min_date = datetime(
                min_year, min_month_zero_based + 1, min_day
            ).date()
            max_date = datetime(
                max_year, max_month_zero_based + 1, max_day
            ).date()
        except ValueError:
            return None

        if min_date > max_date:
            return None
        return min_date, max_date

    def _build_decoy_dates(self, date_str: str) -> list[str]:
        """Build decoy dates within ticketMinDate..ticketMaxDate, excluding target."""
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            self._log(logging.WARNING, f"Invalid target date format: {date_str}")
            return []

        window = self._extract_ticket_date_window()
        if window is None:
            self._log(
                logging.WARNING,
                "ticketMinDate/ticketMaxDate not found in calendar HTML; skipping decoy date probing",
            )
            return []

        min_date, max_date = window
        candidates: list[str] = []

        # Prefer nearby dates first to mimic normal browsing behavior.
        for offset in range(1, (max_date - min_date).days + 1):
            earlier = target_date - timedelta(days=offset)
            later = target_date + timedelta(days=offset)

            if min_date <= earlier <= max_date:
                candidates.append(earlier.strftime("%Y-%m-%d"))
            if min_date <= later <= max_date:
                candidates.append(later.strftime("%Y-%m-%d"))

        # Remove duplicates while preserving order (in case range is tiny).
        seen: set[str] = set()
        deduped = [d for d in candidates if not (d in seen or seen.add(d))]

        self._log(
            logging.DEBUG,
            f"Decoy window {min_date}..{max_date}, candidates={deduped}",
        )
        return deduped

    async def _fetch_timeslots_ajax(
        self, date_str: str, ticket_count: int
    ) -> dict:
        """Fetch timeslots via FlareSolverr browser POST to ``/script/timeslots``.

        Routes the request through the same Chrome browser session that solved
        the Cloudflare challenge, ensuring identical proxy IP and cookies.

        On 403 or error, refreshes the page from ``/tickets`` and retries.
        After a successful fetch, navigates back to the calendar page to
        restore CSRF tokens for form submission.
        """
        import json
        from urllib.parse import urlencode

        # Try to extract product ID from the calendar page
        soup = self.soup
        product_id = "411622"  # default from HAR
        ticket_input = soup.select_one('input[name^="ticketNumbers["]')
        if ticket_input:
            name = str(ticket_input.get("name", ""))
            m = re.search(r"ticketNumbers\[(\d+)\]", name)
            if m:
                product_id = m.group(1)

        # Save the calendar page URL so we can navigate back after fetch
        calendar_url = self.session.last_url or (
            f"{BASE_URL}/en/reservationindividuelle/date"
        )

        payload: Dict[str, str] = {
            "tag": "notredame",
            "eventId": "1",
            "productEventId": "",
            "ticketDate": date_str,
            "ticketNumber": str(ticket_count),
            f"ticketNumbers[{product_id}]": str(ticket_count),
            "timeslotsGroup": "",
            "streetname": "reservationindividuelle",
        }
        post_body = urlencode(payload)
        request_headers = build_ajax_post_headers(calendar_url)
        self._extract_csrf_pair(context_label="calendar_page_before_timeslots_fetch")
        self._log_csrf_usage(
            target_url=TIMESLOTS_URL,
            fields=payload,
            usage_label="calendar_timeslots_ajax",
        )

        for attempt in range(1, TIMESLOT_MAX_RETRIES + 1):
            self._log(
                logging.INFO,
                f"Fetching timeslots via direct session "
                f"(attempt {attempt}/{TIMESLOT_MAX_RETRIES}) "
                f"for date {date_str}",
            )

            try:
                await self.session.post_direct_form(
                    TIMESLOTS_URL,
                    post_data=post_body,
                    timeout=TIMEOUT_FORM_POST,
                    headers=request_headers,
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                requests.RequestException,
            ) as e:
                self._log(
                    logging.WARNING,
                    f"Direct timeslots transport error "
                    f"(attempt {attempt}): {e}",
                )
                await asyncio.sleep(min(10, 3 * attempt))
                continue

            # FlareSolverr wraps the response in HTML; extract raw JSON
            resp_html = self.session.last_html
            status_code = self.session.last_status

            self._log(
                logging.DEBUG,
                f"Timeslots response: status={status_code}, "
                f"len={len(resp_html)}",
            )

            if status_code == 403 or "Just a moment" in resp_html:
                self._log(
                    logging.WARNING,
                    f"Timeslots got CF challenge/403 (attempt {attempt}), "
                    "refreshing page...",
                )
                if self.on_403_refresh:
                    await self.on_403_refresh()
                else:
                    await self.session.get(
                        calendar_url, timeout=TIMEOUT_NAVIGATION
                    )
                await asyncio.sleep(min(10, 3 * attempt))
                continue

            # Try to parse JSON from the response
            # Chrome wraps raw JSON in <pre>...</pre> when displaying it
            json_text = resp_html
            try:
                # Try direct parse first (response may be raw JSON)
                data = json.loads(json_text)
            except (json.JSONDecodeError, TypeError):
                # Extract from HTML wrapper (Chrome's JSON viewer)
                json_soup = parse_html(resp_html)
                pre_tag = json_soup.find("pre")
                if pre_tag:
                    json_text = pre_tag.get_text()
                    try:
                        data = json.loads(json_text)
                    except (json.JSONDecodeError, TypeError):
                        self._log(
                            logging.WARNING,
                            f"Timeslots non-JSON in <pre> "
                            f"(attempt {attempt}): {json_text[:200]}",
                        )
                        await asyncio.sleep(TIMESLOT_RETRY_DELAY)
                        continue
                else:
                    self._log(
                        logging.WARNING,
                        f"Timeslots non-JSON response "
                        f"(attempt {attempt}): {resp_html[:200]}",
                    )
                    # Might be a redirect or error page — refresh
                    if self.on_403_refresh:
                        await self.on_403_refresh()
                    else:
                        await self.session.get(
                            calendar_url, timeout=TIMEOUT_NAVIGATION
                        )
                    await asyncio.sleep(TIMESLOT_RETRY_DELAY)
                    continue

            if data.get("success"):
                self._log(logging.INFO, "Timeslots fetched successfully")
                # Navigate back to calendar page to restore CSRF state
                self._log(
                    logging.DEBUG,
                    "Navigating back to calendar page for CSRF tokens",
                )
                await self.session.get(
                    calendar_url, timeout=TIMEOUT_NAVIGATION
                )
                return data
            else:
                self._log(
                    logging.WARNING,
                    f"Timeslots response success=false "
                    f"(attempt {attempt}): {str(data)[:200]}",
                )

            if attempt < TIMESLOT_MAX_RETRIES:
                await asyncio.sleep(TIMESLOT_RETRY_DELAY)

        raise Exception(
            f"Failed to fetch timeslots after {TIMESLOT_MAX_RETRIES} retries"
        )


    async def get_available_timeslots(
        self, date_str: str, ticket_count: int = 1
    ) -> list[dict]:
        """Fetch and parse available timeslots for the given date.

        Returns a list of ``{"time": "20:15", "class": "timeslotBusy", ...}``.
        When the API returns 0 slots, retries up to TIMESLOT_EMPTY_RETRIES times
        (site may still have availability).
        """
        slots: list[dict] = []
        for empty_attempt in range(TIMESLOT_EMPTY_RETRIES + 1):
            data = await self._fetch_timeslots_ajax(date_str, ticket_count)

            slots = []
            timeslots = data.get("timeslots", {})
            for _key, info in timeslots.items():
                if not isinstance(info, dict):
                    continue
                active = info.get("active", False)
                sold_out = info.get("soldOut", True)
                if active and not sold_out:
                    slots.append(
                        {
                            "time": info.get("time", _key),
                            "class": info.get("classAttr", "timeslotBusy"),
                            "totalAvailable": info.get("totalAvailable", 0),
                        }
                    )

            if slots:
                break
            if empty_attempt < TIMESLOT_EMPTY_RETRIES:
                self._log(
                    logging.INFO,
                    f"Got 0 slots for {date_str}, retrying "
                    f"({empty_attempt + 1}/{TIMESLOT_EMPTY_RETRIES}) in "
                    f"{TIMESLOT_EMPTY_DELAY}s...",
                )
                await asyncio.sleep(TIMESLOT_EMPTY_DELAY)

        self._log(logging.INFO, f"Found {len(slots)} available timeslot(s)")
        for s in slots:
            self._log(
                logging.DEBUG,
                f"  Slot: {s['time']} ({s.get('class', '')}) "
                f"avail={s.get('totalAvailable', '?')}",
            )
        return slots

    async def select_time_and_submit(
        self,
        date_str: str,
        time_str: str,
        ticket_count: int = 1,
        check_availability: bool = True,
    ) -> FlareResponse:
        """Fetch timeslots, validate chosen time, and submit to personal-details.

        Args:
            date_str: Date in ``YYYY-MM-DD`` format.
            time_str: Time to select (e.g. ``"20:15"``).
            ticket_count: Number of tickets.
            check_availability: When false, trust the provided date/time and
                submit directly without fetching timeslots first.
        """
        self._log(logging.INFO, f"Selecting timeslot: {date_str} @ {time_str}")

        if check_availability:
            slots = await self.get_available_timeslots(date_str, ticket_count)

            matching = [s for s in slots if s.get("time") == time_str]
            if not matching:
                available = [s.get("time", "?") for s in slots]
                self._log(
                    logging.WARNING,
                    f"Time {time_str} not in available slots: {available}",
                )
        else:
            self._log(
                logging.INFO,
                "Skipping timeslot availability fetch and trusting broker-provided slot",
            )

        csrf_name, csrf_value = self._extract_csrf_pair(
            context_label="calendar_page_loaded"
        )

        if not csrf_name or not csrf_value:
            self._log(logging.ERROR, "CSRF tokens not found on calendar page")
            raise Exception("CSRF tokens missing from calendar page")

        # Build the POST payload per HAR
        from urllib.parse import urlencode

        fields: Dict[str, str] = {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "ticketDate": date_str,
            "ticketTime": time_str,
        }

        target_url = f"{BASE_URL}/en/reservationindividuelle/personal-details"
        post_body = urlencode(fields)
        request_headers = build_navigation_post_headers(
            self.session.last_url or f"{BASE_URL}/en/reservationindividuelle/date"
        )
        self._log(logging.DEBUG, f"POSTing to {target_url}")
        self._log_csrf_usage(
            target_url=target_url,
            fields=fields,
            usage_label="calendar_to_personal_details_submit",
        )
        res = await self.session.post_direct_form(
            target_url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )
        self._log(logging.INFO, f"Calendar submitted -> {self.session.last_url}")
        await self.save_snapshot("calendar_submitted")
        self._raise_if_invalid_csrf_response(step_label="Calendar submit")

        if not check_availability and not self._is_personal_details_page():
            current_url = self.session.last_url or "<unknown>"
            self._log(
                logging.WARNING,
                "Trusted slot submit did not reach the personal details page "
                f"(url={current_url})",
            )
            raise TrustedSlotSubmissionFailed(
                "Broker-provided slot submit did not reach the personal details page"
            )
        return res

    def _is_personal_details_page(self) -> bool:
        current_url = (self.session.last_url or "").lower()
        if "/personal-details" in current_url:
            return True

        soup = self.soup
        return all(
            soup.select_one(selector) is not None
            for selector in (
                'input[name="firstName"]',
                'input[name="surname"]',
                'input[name="emailAddress"]',
            )
        )

    async def select_best_available_time_and_submit(
        self, date_str: str, ticket_count: int = 1
    ) -> tuple[str, FlareResponse]:
        """CAPTURE_AVAILABLE_TICKETS mode: pick the best unclaimed slot and submit.

        Returns:
            (chosen_time, FlareResponse)
        """
        self._log(logging.INFO, "CAPTURE mode – scanning timeslots…")
        slots = await self.get_available_timeslots(date_str, ticket_count)

        priority_map = {cls: idx for idx, cls in enumerate(self._SLOT_PRIORITY)}
        scored: list[tuple[int, dict]] = []

        for s in slots:
            class_str = s.get("class", "")
            best_cls = "timeslotBusy"
            for cls in self._SLOT_PRIORITY:
                if cls in class_str:
                    best_cls = cls
                    break

            t = s.get("time", "")
            if t in _claimed_time_slots:
                self._log(logging.DEBUG, f"Skipping {t} – claimed")
                continue

            scored.append((priority_map.get(best_cls, 1), s))

        if not scored:
            raise Exception("No available unclaimed timeslots to select")

        random.shuffle(scored)
        scored.sort(key=lambda x: x[0])

        chosen = scored[0][1]
        chosen_time = chosen.get("time", "")
        _claimed_time_slots.add(chosen_time)

        self._log(logging.INFO, f"Best slot: {chosen_time}")
        res = await self.select_time_and_submit(date_str, chosen_time, ticket_count)
        return chosen_time, res


class DetailsPageSession(BasePageSession):
    """Step 3: Fill personal details and POST to /payment.

    HAR POST body::

        csrf_name=csrf6996ac83e32a4
        &csrf_value=29c971b60db6bcf58b67479d543e79cf
        &firstName=James
        &surname=Mcroni
        &zipcode=97220
        &country=US
        &phoneNumber=5031568945
        &phone-number=%2B445031568945
        &emailAddress=jamesmcroni1998@gmail.com
        &emailAddressConfirm=jamesmcroni1998@gmail.com
    """

    async def fill_and_submit(self, user_details: UserDetails) -> FlareResponse:
        """Fill user details into the form and submit to /payment."""
        self._log(logging.INFO, "Filling personal details form")
        self._log(
            logging.DEBUG,
            f"User: {user_details.firstName} {user_details.lastName}",
        )

        await self.save_snapshot("details_page_loaded")

        soup = self.soup
        csrf_name, csrf_value = self._extract_csrf_pair(
            context_label="details_page_loaded"
        )

        if not csrf_name or not csrf_value:
            self._log(logging.ERROR, "CSRF tokens missing from details page")
            raise Exception("CSRF tokens not found on details page")

        # Detect country code from the select options
        country_code = "US"  # default
        country_select = soup.select_one('select[name="country"]')
        if country_select:
            country_lower = user_details.country.lower().strip()
            for opt in country_select.select("option"):
                opt_text = (opt.get_text() or "").strip().lower()
                opt_val = str(opt.get("value", "") or "")
                if opt_text == country_lower or country_lower in opt_text:
                    country_code = opt_val
                    break

        # Build form fields matching exact HAR field names
        from urllib.parse import urlencode

        fields: Dict[str, str] = {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "firstName": user_details.firstName,
            "surname": user_details.lastName,  # HAR uses "surname" not "lastName"
            "zipcode": user_details.zip,  # HAR uses "zipcode" not "zipCode"
            "country": country_code,
            "phoneNumber": user_details.phone,
            "phone-number": f"+44{user_details.phone}",
            "emailAddress": user_details.email,
            "emailAddressConfirm": user_details.email,
        }

        target_url = f"{BASE_URL}/en/reservationindividuelle/payment"
        post_body = urlencode(fields)
        request_headers = build_navigation_post_headers(
            self.session.last_url
            or f"{BASE_URL}/en/reservationindividuelle/personal-details"
        )
        self._log(logging.DEBUG, f"POSTing details to {target_url}")
        self._log_csrf_usage(
            target_url=target_url,
            fields=fields,
            usage_label="details_to_payment_submit",
        )
        res = await self.session.post_direct_form(
            target_url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )
        self._log(logging.INFO, f"Details submitted -> {self.session.last_url}")
        await self.save_snapshot("details_submitted")
        self._raise_if_invalid_csrf_response(step_label="Details submit")
        return res


class DonationPageSession(BasePageSession):
    """Step 4: Skip the donation and POST to /payment.

    HAR POST body::

        csrf_name=csrf6996acd7075b7
        &csrf_value=0bc36e4779cbfcb932f896e3dc259aba
        &donation-input=0
        &donationCheck=true
    """

    async def skip_and_submit(self) -> FlareResponse:
        """Set donation to 0 and submit."""
        self._log(logging.INFO, "Processing donation page (skipping)")
        await self.save_snapshot("donation_page_loaded")

        csrf_name, csrf_value = self._extract_csrf_pair(
            context_label="donation_page_loaded"
        )

        if not csrf_name or not csrf_value:
            self._log(logging.ERROR, "CSRF tokens missing from donation page")
            raise Exception("CSRF tokens not found on donation page")

        from urllib.parse import urlencode

        fields: Dict[str, str] = {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "donation-input": "0",
            "donationCheck": "true",
        }

        target_url = f"{BASE_URL}/en/reservationindividuelle/payment"
        post_body = urlencode(fields)
        request_headers = build_navigation_post_headers(
            self.session.last_url or f"{BASE_URL}/en/reservationindividuelle/donation"
        )
        self._log(logging.DEBUG, f"POSTing donation to {target_url}")
        self._log_csrf_usage(
            target_url=target_url,
            fields=fields,
            usage_label="donation_to_payment_submit",
        )
        res = await self.session.post_direct_form(
            target_url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )
        self._log(logging.INFO, f"Donation skipped -> {self.session.last_url}")
        await self.save_snapshot("donation_submitted")
        self._raise_if_invalid_csrf_response(step_label="Donation submit")
        return res


class PaymentPageSession(BasePageSession):
    """Step 4: Accept terms, solve Turnstile, and POST to /thank-you.

    HAR POST body::

        csrf_name=csrf6996acdd9b159
        &csrf_value=1f31149dde468a614a499ceb4d043af0
        &adyen-data=%5B%5D
        &terms-and-conditions=on
        &cf-turnstile-response=0.9SU_ZFlIVok...
        &paymentCheck=true

    Response: 302 redirect → /en/reservationindividuelle/thank-you?orderHash=…
    """

    async def _refresh_browser_payment_page(self) -> None:
        """Refresh the current payment URL via FlareSolverr using the latest direct-session cookies."""
        target_url = self.session.last_url or f"{BASE_URL}/en/reservationindividuelle/payment"
        previous_state = (
            self.session.last_url,
            self.session.last_status,
            self.session.last_html,
            list(self.session.last_cookies),
            self.session.last_user_agent,
            self.session.last_screenshot_base64,
        )
        self._log(
            logging.INFO,
            f"Refreshing payment page in FlareSolverr browser: {target_url}",
        )
        await self.session.get(
            target_url,
            timeout=TIMEOUT_NAVIGATION,
            headers={"Referer": target_url},
            sync_cookies=True,
        )
        if "/payment" not in (self.session.last_url or "").lower():
            self._log(
                logging.WARNING,
                "Browser refresh did not stay on the payment page; restoring direct-session HTML",
            )
            (
                self.session.last_url,
                self.session.last_status,
                self.session.last_html,
                self.session.last_cookies,
                self.session.last_user_agent,
                self.session.last_screenshot_base64,
            ) = previous_state

    async def accept_terms_and_complete(self) -> FlareResponse:
        """Accept T&C, solve Turnstile via CapSolver, and submit payment."""
        self._log(logging.INFO, "Processing payment page")
        await self._refresh_browser_payment_page()
        await self.save_snapshot("payment_page_loaded")
        self._raise_if_order_limit_reached(step_label="Payment page")

        csrf_name, csrf_value = self._extract_csrf_pair(
            context_label="payment_page_loaded"
        )

        if not csrf_name or not csrf_value:
            self._log(logging.ERROR, "CSRF tokens missing from payment page")
            raise Exception("CSRF tokens not found on payment page")

        # ── Solve Turnstile captcha via CapSolver ─────────────────────
        turnstile_token = await self._solve_turnstile()
        if not turnstile_token:
            self._log(
                logging.WARNING,
                "No Turnstile token obtained — submission may fail",
            )

        # Build form fields matching exact HAR
        from urllib.parse import urlencode

        fields: Dict[str, str] = {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "adyen-data": "[]",
            "terms-and-conditions": "on",
            "paymentCheck": "true",
        }
        if turnstile_token:
            fields["cf-turnstile-response"] = turnstile_token

        target_url = f"{BASE_URL}/en/reservationindividuelle/thank-you"
        post_body = urlencode(fields)
        request_headers = build_navigation_post_headers(
            self.session.last_url or f"{BASE_URL}/en/reservationindividuelle/payment"
        )
        self._log(logging.DEBUG, f"POSTing payment to {target_url}")
        self._log_csrf_usage(
            target_url=target_url,
            fields=fields,
            usage_label="payment_to_thank_you_submit",
        )
        res = await self.session.post_direct_form(
            target_url,
            post_data=post_body,
            timeout=TIMEOUT_FORM_POST,
            headers=request_headers,
        )
        self._log(logging.INFO, f"Payment submitted -> {self.session.last_url}")
        await self.save_snapshot("payment_submitted")
        self._raise_if_invalid_csrf_response(step_label="Payment submit")
        self._raise_if_order_limit_reached(step_label="Payment submit")

        # Check for error in response — may need to re-solve captcha
        html = self.session.last_html
        if "error" in html.lower()[:500] and "thank" not in html.lower()[:500]:
            self._log(
                logging.WARNING,
                "Error detected after payment submission, retrying Turnstile...",
            )
            await self.save_snapshot("payment_error_retry")

            csrf_name, csrf_value = self._extract_csrf_pair(
                context_label="payment_retry_page_loaded"
            )

            turnstile_token = await self._solve_turnstile()
            if turnstile_token:
                fields["csrf_name"] = csrf_name
                fields["csrf_value"] = csrf_value
                fields["cf-turnstile-response"] = turnstile_token
            post_body = urlencode(fields)
            self._log_csrf_usage(
                target_url=target_url,
                fields=fields,
                usage_label="payment_retry_to_thank_you_submit",
            )
            res = await self.session.post_direct_form(
                target_url,
                post_data=post_body,
                timeout=TIMEOUT_FORM_POST,
                headers=request_headers,
            )
            await self.save_snapshot("payment_resubmitted")
            self._raise_if_invalid_csrf_response(step_label="Payment retry submit")

        return res

    async def _solve_turnstile(self) -> str:
        """
        Extract the Turnstile sitekey from the page HTML and solve via CapSolver.

        Returns the solution token or empty string on failure.
        """
        capsolver_api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
        if not capsolver_api_key:
            self._log(logging.ERROR, "CAPSOLVER_API_KEY not set")
            return ""

        soup = self.soup
        site_key = ""

        # Method 1: data-sitekey attribute on .cf-turnstile div
        turnstile_div = soup.select_one(".cf-turnstile, [data-sitekey]")
        if turnstile_div:
            site_key = turnstile_div.get("data-sitekey", "") or ""

        # Method 2: Look in iframe src
        if not site_key:
            iframe = soup.select_one('iframe[src*="turnstile"]')
            if iframe:
                src = iframe.get("src", "")
                if "k=" in str(src):
                    from urllib.parse import urlparse, parse_qs

                    parsed = urlparse(str(src))
                    site_key = parse_qs(parsed.query).get("k", [""])[0]

        # Method 3: Search script tags
        if not site_key:
            import re

            for script in soup.select("script"):
                text = script.get_text()
                match = re.search(r'sitekey["\s:]+["\']([0-9a-zA-Z_-]+)["\']', text)
                if match:
                    site_key = match.group(1)
                    break

        if not site_key:
            site_key = DEFAULT_PAYMENT_TURNSTILE_SITEKEY
            self._log(
                logging.WARNING,
                "Turnstile sitekey not found in page HTML; using fallback sitekey",
            )

        website_url = self.session.last_url
        self._log(logging.INFO, f"Solving Turnstile (sitekey: {site_key[:8]}…)")

        create_payload = {
            "clientKey": capsolver_api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": site_key,
            },
        }

        CAPSOLVER_CREATE_URL = "https://api.capsolver.com/createTask"
        CAPSOLVER_RESULT_URL = "https://api.capsolver.com/getTaskResult"

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as http:
            # Create task
            async with http.post(CAPSOLVER_CREATE_URL, json=create_payload) as resp:
                create_data = await resp.json(content_type=None)

            if create_data.get("errorId", 0) != 0:
                self._log(
                    logging.ERROR,
                    f"CapSolver createTask error: {create_data.get('errorDescription', '')}",
                )
                return ""

            task_id = create_data.get("taskId", "")
            if not task_id:
                self._log(logging.ERROR, "CapSolver returned no taskId")
                return ""

            self._log(logging.DEBUG, f"CapSolver task created: {task_id}")

            # Poll for result
            for poll in range(90):
                await asyncio.sleep(2)
                async with http.post(
                    CAPSOLVER_RESULT_URL,
                    json={"clientKey": capsolver_api_key, "taskId": task_id},
                ) as resp:
                    result_data = await resp.json(content_type=None)

                status = result_data.get("status", "")
                if status == "ready":
                    token = (
                        result_data.get("solution", {}).get("token", "")
                    )
                    self._log(
                        logging.INFO,
                        f"Turnstile solved (token: {token[:20]}…)",
                    )
                    return token
                elif status == "failed":
                    self._log(
                        logging.ERROR,
                        f"CapSolver task failed: {result_data.get('errorDescription', '')}",
                    )
                    return ""

        self._log(logging.ERROR, "CapSolver polling timed out")
        return ""


class ConfirmationPageSession(BasePageSession):
    """Step 5: Verify the booking confirmation (thank-you page).

    After payment POST, the server returns a 302 redirect to
    ``/en/reservationindividuelle/thank-you?orderHash=…``
    FlareSolverr follows the redirect automatically.  We just check the
    confirmation text on the resulting page.
    """

    async def verify_confirmation(self) -> bool:
        """Check for confirmation text on the thank-you page.

        Returns ``True`` if the reservation appears confirmed.
        """
        self._log(logging.INFO, "Checking confirmation page")
        await self.save_snapshot("confirmation_page_loaded")

        soup = self.soup
        body_text = soup.get_text(separator=" ", strip=True).lower()

        # Look for confirmation indicators
        confirmed = any(
            phrase in body_text
            for phrase in [
                "reservation is confirmed",
                "booking is confirmed",
                "thank you for your reservation",
                "your reservation",
                "orderhash",
                "confirmed",
            ]
        )

        if confirmed:
            self._log(logging.INFO, "Reservation confirmed!")
        else:
            self._log(
                logging.WARNING,
                "Confirmation text not found on page. "
                f"URL: {self.session.last_url}",
            )
            # Check if the URL itself contains orderHash (strong signal)
            if "orderHash" in self.session.last_url:
                self._log(
                    logging.INFO,
                    "orderHash found in URL — likely confirmed",
                )
                confirmed = True

        return confirmed


# ---------------------------------------------------------------------------
# run_instance — main orchestration function
# ---------------------------------------------------------------------------


async def run_instance(
    user_details: UserDetails,
    instance_id: int = 0,
    instance_status: dict | None = None,
    flaresolverr_url: str | None = None,
    flaresolverr_urls: list[str] | None = None,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> None:
    """
    Run a single bot instance entirely through a FlareSolverr session.

    Args:
        user_details: Booking information.
        instance_id:  Instance identifier for logging.
        instance_status: Shared dict for stage tracking (multiprocessing).
    """
    run_metadata = run_metadata or {}
    log_parts = [f"Instance {instance_id}"]
    if run_metadata.get("worker_id"):
        log_parts.append(f"Worker {run_metadata['worker_id']}")
    if run_metadata.get("task_id"):
        log_parts.append(f"Task {run_metadata['task_id']}")
    log_prefix = "][".join(log_parts)

    def instance_logger(msg: str, level: int = logging.INFO) -> None:
        logger.log(level, f"[{log_prefix}] {msg}")

    current_stage = ""
    skip_slot_availability_check = False

    def _stage(label: str) -> None:
        nonlocal current_stage
        current_stage = label
        _update_stage(
            instance_id,
            label,
            instance_status,
            status_callback=status_callback,
        )

    _stage("Initialising")

    # Validate
    try:
        validate_user_details(user_details)
        instance_logger("User details validation passed")
    except ValueError as e:
        instance_logger(f"Validation failed: {e}", logging.ERROR)
        _finish_stage(
            instance_id,
            "error",
            instance_status,
            str(e),
            status_callback=status_callback,
            stage=current_stage,
        )
        raise

    upstream_proxy = user_details.upstream_proxy or user_details.proxy or UPSTREAM_PROXY
    instance_logger(
        f"Starting bot for {user_details.firstName} {user_details.lastName}"
    )
    instance_logger(f"Upstream proxy: {upstream_proxy}", logging.DEBUG)
    if run_metadata:
        instance_logger(f"Run metadata: {run_metadata}", logging.INFO)

    resolved_flaresolverr_urls = get_flaresolverr_urls(
        explicit_url=flaresolverr_url,
        explicit_urls=flaresolverr_urls,
    )
    if not resolved_flaresolverr_urls:
        raise RuntimeError("No FlareSolverr URLs are available")

    # Round-robin across FlareSolverr instances by instance_id
    # (can't rely on itertools.cycle since each multiprocessing.Process
    #  gets its own copy of the iterator, always starting at index 0)
    flare_url_index = 0 if flaresolverr_url else instance_id % len(
        resolved_flaresolverr_urls
    )
    flare_url = resolved_flaresolverr_urls[flare_url_index]
    instance_logger(f"Using FlareSolverr URL: {flare_url}")
    navigation_recoveries = 0

    session = FlareSession(
        flaresolverr_url=flare_url,
        upstream_proxy=upstream_proxy,
        instance_id=instance_id,
    )

    async def recover_navigation_session(
        reason: str,
        rotate_proxy: bool = False,
    ) -> None:
        nonlocal session
        nonlocal upstream_proxy
        nonlocal flare_url
        nonlocal flare_url_index
        nonlocal navigation_recoveries

        if navigation_recoveries >= NAVIGATION_SESSION_RECOVERY_LIMIT:
            raise RuntimeError(
                "Navigation recovery limit reached "
                f"({NAVIGATION_SESSION_RECOVERY_LIMIT}): {reason}"
            )

        navigation_recoveries += 1
        instance_logger(
            f"Recovering navigation session "
            f"({navigation_recoveries}/{NAVIGATION_SESSION_RECOVERY_LIMIT}) "
            f"because: {reason}",
            logging.WARNING,
        )

        try:
            await session.close()
        except Exception as close_exc:
            instance_logger(
                f"Previous session close failed during recovery: {close_exc}",
                logging.DEBUG,
            )

        if rotate_proxy:
            rotated_proxy = rotate_proxy_session(upstream_proxy)
            if rotated_proxy != upstream_proxy:
                upstream_proxy = rotated_proxy
                instance_logger(
                    "Rotated proxy sticky session token for a fresh exit IP",
                    logging.INFO,
                )

        flare_url_index = (flare_url_index + 1) % len(resolved_flaresolverr_urls)
        flare_url = resolved_flaresolverr_urls[flare_url_index]
        instance_logger(f"Switching FlareSolverr URL to: {flare_url}")

        session = FlareSession(
            flaresolverr_url=flare_url,
            upstream_proxy=upstream_proxy,
            instance_id=instance_id,
        )
        await session.create()
        instance_logger(f"Recovery session created: {session.session_id}")

    try:
        # ── Create session ────────────────────────────────────────────
        _stage("Creating FlareSolverr session")
        try:
            await session.create()
        except FlareSession.RequestError as create_err:
            await recover_navigation_session(
                reason=f"sessions.create HTTP {create_err.status_code}",
                rotate_proxy=create_err.is_cloudflare_block,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as create_exc:
            await recover_navigation_session(
                reason=f"sessions.create transport error: {create_exc}",
                rotate_proxy=False,
            )
        instance_logger(f"Session ID: {session.session_id}")

        # ── Navigate to reservation (Cloudflare solved automatically) ─
        _stage("Navigating to reservation page (solving CF)")
        instance_logger("Navigating to reservation page...")

        MAX_NAV_RETRIES = 3 + NAVIGATION_SESSION_RECOVERY_LIMIT
        for nav_attempt in range(1, MAX_NAV_RETRIES + 1):
            instance_logger(f"Navigation attempt {nav_attempt}/{MAX_NAV_RETRIES}")

            try:
                res = await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)
            except FlareSession.RequestError as req_err:
                instance_logger(
                    f"FlareSolverr request failed "
                    f"(HTTP {req_err.status_code}): {req_err.body[:220]}",
                    logging.WARNING,
                )
                if nav_attempt < MAX_NAV_RETRIES:
                    await recover_navigation_session(
                        reason=f"HTTP {req_err.status_code}",
                        rotate_proxy=req_err.is_cloudflare_block,
                    )
                    await asyncio.sleep(min(8, nav_attempt * 2))
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as req_exc:
                instance_logger(
                    f"Navigation transport error: {req_exc}",
                    logging.WARNING,
                )
                if nav_attempt < MAX_NAV_RETRIES:
                    await recover_navigation_session(
                        reason=str(req_exc),
                        rotate_proxy=False,
                    )
                    await asyncio.sleep(min(8, nav_attempt * 2))
                    continue
                raise

            if not res.ok:
                instance_logger(
                    f"FlareSolverr returned non-ok: {res.message}",
                    logging.WARNING,
                )
                if nav_attempt < MAX_NAV_RETRIES:
                    if is_cf_block_message(str(res.message)):
                        await recover_navigation_session(
                            reason=str(res.message),
                            rotate_proxy=True,
                        )
                    await asyncio.sleep(3 * nav_attempt)
                    continue
                raise Exception(
                    f"FlareSolverr failed after {MAX_NAV_RETRIES} attempts: {res.message}"
                )

            # Check if CF was bypassed
            page_html = session.last_html
            if "Just a moment" in page_html or "Checking your browser" in page_html:
                instance_logger(
                    f"Still on CF challenge page (attempt {nav_attempt})",
                    logging.WARNING,
                )
                if nav_attempt < MAX_NAV_RETRIES:
                    if nav_attempt >= 2:
                        await recover_navigation_session(
                            reason="challenge page persisted after successful GET",
                            rotate_proxy=False,
                        )
                    await asyncio.sleep(5 * nav_attempt)
                    continue
                raise Exception("Cloudflare challenge not bypassed")

            instance_logger(
                f"Reservation page loaded -> {session.last_url} "
                f"(cookies: {len(session.last_cookies)}, "
                f"cf_clearance={any(c.name == 'cf_clearance' for c in session.last_cookies)})"
            )
            break

        # ── Wait for job_time ─────────────────────────────────────────
        paris_tz = ZoneInfo("Europe/Paris")
        job_time_str = user_details.job_time

        if job_time_str and job_time_str != "00:00":
            job_hour, job_minute = map(int, job_time_str.split(":"))
            now_paris = datetime.now(paris_tz)
            target_time = now_paris.replace(
                hour=job_hour, minute=job_minute, second=0, microsecond=0
            )
            if target_time <= now_paris:
                target_time += timedelta(days=1)

            seconds_until_job = (target_time - now_paris).total_seconds()
            if seconds_until_job > 0:
                instance_logger(
                    f"Job scheduled for {job_time_str} Paris time. "
                    f"Waiting {seconds_until_job:.0f}s..."
                )
                # Keep session alive by re-GETing periodically
                remaining = seconds_until_job
                while remaining > 0:
                    sleep_time = min(120, remaining)  # Ping every 2 min max
                    await asyncio.sleep(sleep_time)
                    remaining -= sleep_time
                    if remaining > 0:
                        # Keep-alive: re-navigate to prevent session timeout
                        instance_logger(
                            f"Keep-alive ping ({remaining:.0f}s remaining)...",
                            logging.DEBUG,
                        )
                        await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)

                instance_logger("Job time reached!")
                # Re-navigate fresh for the booking
                await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)
        else:
            instance_logger("No job_time set, proceeding immediately")

        # ── Steps 1+2: Tickets + Calendar (with session recovery on failure) ─
        MAX_CALENDAR_RETRIES = CALENDAR_RETRY_ATTEMPTS
        chosen_time = ""
        for calendar_attempt in range(1, MAX_CALENDAR_RETRIES + 1):
            try:
                # ── Step 1: Home Page — select tickets ────────────────
                _stage("STEP 1/5: Tickets Page")
                instance_logger("STEP 1/5: Tickets Page - Selecting tickets")
                home_page = HomePageSession(session, instance_id)
                await home_page.wait_for_load()
                await home_page.select_tickets_and_submit(user_details.ticket_count)
                await asyncio.sleep(random.uniform(1, 3))

                # ── Step 2: Calendar Page — fetch timeslots + submit ──
                _stage("STEP 2/5: Calendar Page")
                instance_logger(
                    "STEP 2/5: Calendar Page - Fetching timeslots & submitting"
                )

                async def refresh_from_tickets() -> None:
                    await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)
                    await home_page.wait_for_load()
                    await home_page.select_tickets_and_submit(
                        user_details.ticket_count
                    )

                calendar_page = CalendarPageSession(
                    session, instance_id, on_403_refresh=refresh_from_tickets
                )
                date_str = str(user_details.date)

                if CAPTURE_AVAILABLE_TICKETS:
                    instance_logger("CAPTURE mode – picking best slot")
                    chosen_time, _ = (
                        await calendar_page.select_best_available_time_and_submit(
                            date_str, user_details.ticket_count
                        )
                    )
                    instance_logger(f"Auto-selected time: {chosen_time}")
                else:
                    await calendar_page.select_time_and_submit(
                        date_str, user_details.time, user_details.ticket_count
                    )
                break  # Success — exit retry loop

            except Exception as cal_err:
                instance_logger(
                    f"Calendar step failed (attempt {calendar_attempt}/"
                    f"{MAX_CALENDAR_RETRIES}): {cal_err}",
                    logging.WARNING,
                )
                if calendar_attempt < MAX_CALENDAR_RETRIES:
                    instance_logger(
                        "Recovering session with rotated proxy and retrying "
                        "from step 1...",
                        logging.WARNING,
                    )
                    await recover_navigation_session(
                        reason=f"Calendar step failed: {cal_err}",
                        rotate_proxy=True,
                    )
                    # Re-navigate through CF with the new session
                    _stage("Re-navigating after session recovery")
                    for nav_retry in range(1, 4):
                        try:
                            res = await session.get(
                                RESERVATION_URL, timeout=TIMEOUT_NAVIGATION
                            )
                            page_html = session.last_html
                            if (
                                "Just a moment" not in page_html
                                and "Checking your browser" not in page_html
                            ):
                                instance_logger(
                                    "Re-navigation successful after recovery"
                                )
                                break
                        except Exception as nav_err:
                            instance_logger(
                                f"Re-navigation failed ({nav_retry}/3): {nav_err}",
                                logging.WARNING,
                            )
                            if nav_retry < 3:
                                await asyncio.sleep(5)
                    continue
                else:
                    raise  # Exhausted calendar retries, propagate error

        await asyncio.sleep(random.uniform(2, 5))

        # ── Step 3: Details Page — fill user info ─────────────────────
        _stage("STEP 3/5: Personal Details")
        instance_logger("STEP 3/5: Personal Details - Filling information")
        details_page = DetailsPageSession(session, instance_id)
        await details_page.fill_and_submit(user_details)
        await asyncio.sleep(random.uniform(2, 4))

        # ── Step 4: Payment Page — Turnstile + submit ─────────────────
        _stage("STEP 4/5: Payment Page")
        instance_logger("STEP 4/5: Payment Page - Solving captcha & submitting")
        payment_page = PaymentPageSession(session, instance_id)
        await payment_page.accept_terms_and_complete()
        await asyncio.sleep(5)

        # ── Step 5: Confirmation ──────────────────────────────────────
        _stage("STEP 5/5: Confirmation")
        instance_logger("STEP 5/5: Checking confirmation")
        confirmation_page = ConfirmationPageSession(session, instance_id)
        confirmed = await confirmation_page.verify_confirmation()

        if confirmed:
            instance_logger("Reservation confirmed successfully!")
            _finish_stage(
                instance_id,
                "success",
                instance_status,
                status_callback=status_callback,
                stage=current_stage,
            )
        else:
            confirmation_artifact_name = build_artifact_name(
                "confirmation",
                user_details,
                run_metadata,
            )
            await session.save_screenshot(confirmation_artifact_name)
            instance_logger(
                "Confirmation not detected — check screenshots",
                logging.WARNING,
            )
            _finish_stage(
                instance_id,
                "error",
                instance_status,
                "Confirmation text not found",
                status_callback=status_callback,
                stage=current_stage,
            )

        instance_logger("Bot completed successfully")

    except KeyboardInterrupt:
        _finish_stage(
            instance_id,
            "interrupted",
            instance_status,
            status_callback=status_callback,
            stage=current_stage,
        )
        instance_logger("Interrupted by user", logging.WARNING)

    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)[:120]
        _finish_stage(
            instance_id,
            "error",
            instance_status,
            f"{error_type}: {error_msg}",
            status_callback=status_callback,
            stage=current_stage,
        )
        instance_logger(f"Error ({error_type}): {e}", logging.ERROR)
        logger.exception(f"[Instance {instance_id}] Full traceback:")

        # Save error snapshot
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = SCREENSHOTS_DIR / f"flare_{instance_id}_{timestamp}_ERROR_{error_type}.html"
            fname.write_text(session.last_html[:500_000], encoding="utf-8")
            instance_logger(f"Error snapshot: {fname}", logging.DEBUG)
        except Exception:
            pass

    finally:
        instance_logger("Cleaning up session...")
        await session.close()
        instance_logger("Session closed")


# ---------------------------------------------------------------------------
# Process wrapper (for multiprocessing)
# ---------------------------------------------------------------------------


def run_bot_process(
    user_details: UserDetails,
    instance_id: int,
    instance_status: dict | None = None,
    flaresolverr_urls: list[str] | None = None,
) -> None:
    """Run the async bot instance in a separate process."""
    try:
        asyncio.run(
            run_instance(
                user_details,
                instance_id,
                instance_status=instance_status,
                flaresolverr_urls=flaresolverr_urls,
            )
        )
    except KeyboardInterrupt:
        _finish_stage(instance_id, "interrupted", instance_status)
        logger.warning(f"[Process {instance_id}] KeyboardInterrupt")
    except Exception as e:
        _finish_stage(instance_id, "error", instance_status, str(e)[:120])
        logger.error(f"[Process {instance_id}] Process error: {e}")
        logger.exception(f"[Process {instance_id}] Full traceback:")


# ---------------------------------------------------------------------------
# main() — standalone entry point (mirrors main_.py)
# ---------------------------------------------------------------------------


async def main(
    email_provider: str = "burner",
    flaresolverr_urls: list[str] | None = None,
) -> None:
    """
    Main entry point for local testing with generated user details.
    For production use with Redis queue, use start_flare.py instead.
    """
    logger.info("=" * 60)
    logger.info("FLARE BOT STARTED (SESSION MODE)")
    logger.info("=" * 60)
    logger.info("Email provider: %s", email_provider)

    num_instances = int(os.getenv("BOT_NUM_INSTANCES", "1"))
    resolved_flaresolverr_urls = get_flaresolverr_urls(explicit_urls=flaresolverr_urls)
    if not resolved_flaresolverr_urls:
        logger.error("No FlareSolverr URLs resolved; cannot continue.")
        return
    logger.info(
        "Resolved %d FlareSolverr URL(s) for this run",
        len(resolved_flaresolverr_urls),
    )

    mp_manager = multiprocessing.Manager()
    instance_status = mp_manager.dict()

    # ── Validate proxies ──────────────────────────────────────────────
    if not UPSTREAM_PROXIES:
        logger.error("No proxies loaded from %s – cannot continue.", PROXIES_FILE)
        return

    logger.info(
        "Validating proxies from pool of %d (need %d)...",
        len(UPSTREAM_PROXIES),
        num_instances,
    )
    try:
        proxies = await get_validated_proxies(
            needed=num_instances,
            all_proxies=UPSTREAM_PROXIES,
        )
    except RuntimeError as e:
        logger.error(str(e))
        return

    try:
        # Pre-generate user details for all instances
        all_user_details: list[UserDetails] = []
        burner_emails: list[str] = []
        if email_provider == "burner":
            burner_emails = await claim_burner_emails(num_instances)

        for i in range(num_instances):
            fake_details = get_fake_details(seed=int(random.uniform(1000, 9999)))

            if email_provider == "burner":
                alias_email = burner_emails[i]
            else:
                # Create email alias via chosen provider
                alias_email = await create_alias(
                    email_provider,
                    first_name=fake_details["firstName"],
                    last_name=fake_details["lastName"],
                )
            logger.info(
                "[Instance %d] %s email: %s", i, email_provider, alias_email
            )
            # Delay between API alias creations to avoid rate limits
            if email_provider in {"simplelogin", "addy"} and i < num_instances - 1:
                await asyncio.sleep(2)

            ud = UserDetails(
                firstName=fake_details["firstName"],
                lastName=fake_details["lastName"],
                zip=os.getenv("BOT_ZIP", "97220"),
                country=os.getenv("BOT_COUNTRY", "United States Of America"),
                phone=fake_details["phone"],
                email=alias_email,
                unique_id=f"user{i}",
                date=os.getenv("BOT_DATE", "2026-01-18"),
                time=os.getenv("BOT_TIME", "15:45"),
                ticket_count=int(os.getenv("BOT_TICKET_COUNT", "2")),
                job_time=os.getenv("BOT_JOB_TIME", "00:00"),
                status="pending",
                proxy="",  # Not needed — FlareSolverr handles proxy
                upstream_proxy=proxies[i],
            )
            all_user_details.append(ud)

        processes: list[multiprocessing.Process] = []

        for i in range(num_instances):
            logger.info(
                f"[Instance {i}] Launching with proxy {proxies[i].split(':')[0]}:***"
            )
            p = multiprocessing.Process(
                target=run_bot_process,
                args=(
                    all_user_details[i],
                    i,
                    instance_status,
                    resolved_flaresolverr_urls,
                ),
            )
            p.start()
            processes.append(p)

            if i < num_instances - 1:
                await asyncio.sleep(INSTANCE_STAGGER_DELAY)

        for p in processes:
            p.join()

    except KeyboardInterrupt:
        logger.warning("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
        logger.exception("Full traceback:")
    finally:
        print_run_summary(dict(instance_status), num_instances, logger)

        try:
            mp_manager.shutdown()
        except Exception:
            pass

        logger.info("=" * 60)
        logger.info("FLARE BOT FINISHED")
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FlareSolverr session-based bot")
    parser.add_argument(
        "--email-provider",
        choices=VALID_PROVIDERS,
        default="burner",
        help=(
            "Email provider to use: "
            "burner (claim emails from the burner pool API), "
            "faker (offline fake emails), "
            "simplelogin (requires SIMPLELOGIN_API_KEY), "
            "addy (requires ADDY_API_KEY). "
            "Default: burner"
        ),
    )
    # Keep --fake-email as a hidden shortcut for backwards compat
    parser.add_argument(
        "--fake-email",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--flaresolverr-url",
        default="",
        help="Explicit FlareSolverr URL for this run.",
    )
    parser.add_argument(
        "--flaresolverr-urls",
        default="",
        help="Comma-separated FlareSolverr URL pool override.",
    )
    args = parser.parse_args()

    provider = args.email_provider
    if args.fake_email:
        provider = "faker"

    explicit_pool = split_csv_urls(args.flaresolverr_urls)
    if args.flaresolverr_url.strip():
        explicit_pool = [args.flaresolverr_url.strip()]

    asyncio.run(
        main(
            email_provider=provider,
            flaresolverr_urls=explicit_pool or None,
        )
    )
