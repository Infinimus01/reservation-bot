from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
import requests
from dotenv import load_dotenv
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

from flare_bot import (
    PROXIES_FILE,
    RESERVATION_URL,
    TIMESLOTS_URL,
    TIMEOUT_FORM_POST,
    TIMEOUT_NAVIGATION,
    FlareSession,
    HomePageSession,
    build_ajax_post_headers,
    build_navigation_post_headers,
    check_proxy,
    get_flaresolverr_urls,
    get_validated_proxies,
    load_proxies_from_file,
    parse_html,
    setup_logging,
)
from shared.config import AvailabilityCheckerSettings
from shared.iproyal_proxy import (
    IPRoyalProxySettings,
    acquire_warmed_iproyal_proxy,
    proxy_display,
)
from shared.models import AvailabilityTriggerRequest


disable_warnings(InsecureRequestWarning)

logger = logging.getLogger("flare_bot.availability_checker")
DISCOVERY_TICKET_COUNT = 1
CALENDAR_ENTRY_MAX_ATTEMPTS = 3
TIMESLOTS_403_RETRY_ATTEMPTS = 2


def extract_datadome_captcha_url(html: str) -> str | None:
    import re
    # Match src="https://geo.captcha-delivery.com/captcha/?..."
    match = re.search(r'src=["\'](https://geo\.captcha-delivery\.com/captcha/[^"\']+)["\']', html)
    if match:
        return match.group(1)
    # Match URL directly inside script/text
    match = re.search(r'(https://geo\.captcha-delivery\.com/captcha/[^"\s\')]+)', html)
    if match:
        return match.group(1)
    return None


async def solve_datadome_2captcha(
    website_url: str,
    captcha_url: str,
    user_agent: str,
    proxy_line: str,
) -> str | None:
    import os
    import aiohttp
    import asyncio

    api_key = os.getenv("TWOCAPTCHA_API_KEY") or os.getenv("TWO_CAPTCHA_API_KEY")
    if not api_key:
        logger.error("2captcha API key (TWOCAPTCHA_API_KEY) is not set in environment.")
        return None

    if not proxy_line:
        logger.error("Proxy is required for DataDome solving.")
        return None

    parts = proxy_line.split(":")
    if len(parts) < 2:
        logger.error(f"Invalid proxy format: {proxy_line}")
        return None

    host, port = parts[0], parts[1]
    username, password = None, None
    if len(parts) >= 4:
        username, password = parts[2], parts[3]

    task = {
        "type": "DataDomeSliderTask",
        "websiteURL": website_url,
        "captchaUrl": captcha_url,
        "userAgent": user_agent,
        "proxyType": "http",
        "proxyAddress": host,
        "proxyPort": int(port),
    }
    if username and password:
        task["proxyLogin"] = username
        task["proxyPassword"] = password

    payload = {
        "clientKey": api_key,
        "task": task
    }

    create_url = "https://api.2captcha.com/createTask"
    result_url = "https://api.2captcha.com/getTaskResult"

    logger.info(f"Submitting DataDomeSliderTask to 2captcha. websiteURL: {website_url}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(create_url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"2captcha createTask HTTP {resp.status}: {text}")
                    return None
                data = await resp.json()

            if data.get("errorId", 0) != 0:
                logger.error(f"2captcha createTask error: {data.get('errorDescription')}")
                return None

            task_id = data.get("taskId")
            if not task_id:
                logger.error("2captcha did not return taskId")
                return None

            logger.info(f"2captcha task created. ID: {task_id}. Polling for result...")

            # Poll for result
            for _ in range(60):
                await asyncio.sleep(3)
                async with session.post(result_url, json={
                    "clientKey": api_key,
                    "taskId": task_id
                }) as resp:
                    if resp.status != 200:
                        continue
                    res_data = await resp.json()

                if res_data.get("errorId", 0) != 0:
                    logger.error(f"2captcha getTaskResult error: {res_data.get('errorDescription')}")
                    return None

                if res_data.get("status") == "ready":
                    solution = res_data.get("solution", {})
                    cookie_str = solution.get("cookie")
                    logger.info("2captcha successfully solved DataDome captcha")
                    return cookie_str

            logger.error("2captcha solving timed out.")
        except Exception as e:
            logger.error(f"Exception during 2captcha solve: {e}")

    return None


def is_datadome_or_protection_page(html: str) -> bool:
    markers = [
        "Verification Required",
        "Slide right to secure your access",
        "Access is temporarily restricted",
        "Please enable JS",
        "api-js.datadome.co",
        'id="cmsg"',
        "var dd=",
    ]
    return any(marker in html for marker in markers)


async def solve_datadome_in_session_if_needed(session: FlareSession, website_url: str, upstream_proxy: str) -> bool:
    html = session.last_html or ""
    if not is_datadome_or_protection_page(html):
        return False

    logger.info("DataDome protection detected on FlareSolverr page. Attempting to solve via 2captcha...")

    captcha_url = extract_datadome_captcha_url(html)
    if not captcha_url:
        logger.error("Could not find captcha-delivery URL in HTML.")
        return False

    if "t=bv" in captcha_url:
        logger.error(
            "DataDome captcha URL contains 't=bv'. This means the current proxy IP is banned by DataDome. "
            "Solver cannot proceed with this IP. Please change/rotate the proxy IP."
        )
        return False

    user_agent = session.last_user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    cookie_str = await solve_datadome_2captcha(
        website_url=website_url,
        captcha_url=captcha_url,
        user_agent=user_agent,
        proxy_line=upstream_proxy,
    )

    if not cookie_str:
        logger.error("Failed to obtain solution cookie from 2captcha.")
        return False

    # Extract datadome cookie value
    import re
    match = re.search(r'datadome=([^;]+)', cookie_str)
    if not match:
        logger.error(f"Could not parse datadome value from 2captcha solution: {cookie_str}")
        return False

    datadome_val = match.group(1)

    # Add/Update the cookie in session.last_cookies
    from flare_bot import FlareCookie
    session.last_cookies = [c for c in session.last_cookies if c.name != "datadome"]

    session.last_cookies.append(
        FlareCookie(
            name="datadome",
            value=datadome_val,
            domain="resa.notredamedeparis.fr",
            path="/",
            secure=True,
            http_only=False,
            same_site="Lax"
        )
    )
    session.last_cookies.append(
        FlareCookie(
            name="datadome",
            value=datadome_val,
            domain=".notredamedeparis.fr",
            path="/",
            secure=True,
            http_only=False,
            same_site="Lax"
        )
    )

    logger.info("Added solved datadome cookie to FlareSolverr session cookies list. Reloading page with solved cookie...")
    await session.get(website_url, timeout=TIMEOUT_NAVIGATION, sync_cookies=True)
    return True



class TimeslotsForbiddenError(RuntimeError):
    """Raised when the timeslots endpoint returns HTTP 403 for the current session."""

    def __init__(self, date_str: str, message: str | None = None) -> None:
        self.date_str = date_str
        super().__init__(message or f"Timeslots request returned HTTP 403 for {date_str}")


class AvailabilityBackoffError(RuntimeError):
    """Raised to abort the current cycle and wait for the next poll interval."""


def normalize_trigger_date(date_value: str) -> str:
    return datetime.strptime(date_value, "%Y-%m-%d").strftime("%Y/%m/%d")


def build_cycle_report_filename(run_started_at: datetime) -> str:
    return f"availability_{run_started_at.strftime('%Y-%m-%d_%H-%M-%S')}.json"


def _coerce_slot_quantity(raw_value: object) -> int:
    if isinstance(raw_value, bool):
        return int(raw_value)
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, float):
        return int(raw_value)

    text = str(raw_value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return 0


def _extract_calendar_date_window(html: str) -> tuple[date, date] | None:
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
        min_date = date(
            int(min_match.group(1)),
            int(min_match.group(2)) + 1,
            int(min_match.group(3)),
        )
        max_date = date(
            int(max_match.group(1)),
            int(max_match.group(2)) + 1,
            int(max_match.group(3)),
        )
    except ValueError:
        return None

    if min_date > max_date:
        return None
    return min_date, max_date


def _extract_calendar_string_array(html: str, variable_name: str) -> list[str]:
    match = re.search(
        rf"var\s+{re.escape(variable_name)}\s*=\s*(\[[^\]]*\])\s*;",
        html,
    )
    if not match:
        return []

    literal = match.group(1).strip().replace("'", '"')
    try:
        values = json.loads(literal)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return sorted(result)


def _extract_calendar_number_array(html: str, variable_name: str) -> set[int]:
    match = re.search(
        rf"var\s+{re.escape(variable_name)}\s*=\s*(\[[^\]]*\])\s*;",
        html,
    )
    if not match:
        return set()

    literal = match.group(1).strip().replace("'", '"')
    try:
        values = json.loads(literal)
    except json.JSONDecodeError:
        return set()
    if not isinstance(values, list):
        return set()

    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _extract_ticket_product_id(html: str) -> str:
    soup = parse_html(html)
    for selector in ('select[name^="tickets["]', 'input[name^="ticketNumbers["]'):
        element = soup.select_one(selector)
        if element is None:
            continue
        name = str(element.get("name", "") or "")
        match = re.search(r"\[(\d+)\]", name)
        if match:
            return match.group(1)

    match = re.search(r'var\s+ticketNumbers\s*=\s*\{\s*"(\d+)"\s*:', html)
    if match:
        return match.group(1)
    return "411622"


def extract_available_dates_from_calendar_html(html: str) -> list[str]:
    soup = parse_html(html)
    available_dates: list[str] = []
    seen: set[str] = set()

    for cell in soup.select(
        'td[data-handler="selectDay"][data-event="click"][data-month][data-year]'
    ):
        anchor = cell.select_one("a")
        if anchor is None:
            continue

        day_text = anchor.get_text(strip=True)
        if not day_text.isdigit():
            continue

        try:
            resolved_date = date(
                int(str(cell.get("data-year", "")).strip()),
                int(str(cell.get("data-month", "")).strip()) + 1,
                int(day_text),
            ).strftime("%Y-%m-%d")
        except ValueError:
            continue

        if resolved_date in seen:
            continue
        seen.add(resolved_date)
        available_dates.append(resolved_date)

    if available_dates:
        return sorted(available_dates)

    open_dates = _extract_calendar_string_array(html, "openDates")
    if open_dates:
        return open_dates

    window = _extract_calendar_date_window(html)
    if window is None:
        return []

    disabled_dates = set(_extract_calendar_string_array(html, "disabledDates"))
    soldout_dates = set(_extract_calendar_string_array(html, "soldoutDates"))
    disabled_weekdays = _extract_calendar_number_array(html, "disabledWeekDays")

    current = window[0]
    while current <= window[1]:
        current_str = current.strftime("%Y-%m-%d")
        js_weekday = (current.weekday() + 1) % 7
        if (
            current_str not in disabled_dates
            and current_str not in soldout_dates
            and js_weekday not in disabled_weekdays
        ):
            available_dates.append(current_str)
        current += timedelta(days=1)

    return available_dates


def extract_available_slots_from_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    timeslots = payload.get("timeslots", {})
    if not isinstance(timeslots, dict):
        return slots

    for key, info in timeslots.items():
        if not isinstance(info, dict):
            continue

        time_value = str(info.get("time", key)).strip()
        quantity = _coerce_slot_quantity(info.get("totalAvailable", 0))
        if not time_value or quantity <= 0:
            continue
        if info.get("soldOut") is True:
            continue

        slots.append(
            {
                "time": time_value,
                "class": info.get("classAttr", ""),
                "totalAvailable": quantity,
                "active": bool(info.get("active", False)),
            }
        )

    slots.sort(key=lambda slot: str(slot.get("time", "")))
    return slots


def is_calendar_page_html(html: str) -> bool:
    if not html.strip():
        return False
    if "Invalid CSRF token provided" in html:
        return False

    soup = parse_html(html)
    if soup.select_one(".ui-datepicker-calendar") is not None:
        return True
    if "ticketMinDate" in html and "ticketMaxDate" in html:
        return True
    return False


def _proxy_url_from_upstream(proxy_value: str) -> str | None:
    if not proxy_value:
        return None
    parts = proxy_value.split(":")
    if len(parts) < 4:
        return None
    host, port, username = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    return f"http://{username}:{password}@{host}:{port}"


class ReservationDirectSession:
    def __init__(self, flare_session: FlareSession):
        proxy_url = _proxy_url_from_upstream(flare_session.upstream_proxy)
        cffi_proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}

        _use_cffi = False
        try:
            from curl_cffi import requests as _cffi_requests
            # Proxy must go in the constructor — curl_cffi ignores .proxies.update()
            self._http = _cffi_requests.Session(impersonate="chrome124", proxies=cffi_proxies)
            _use_cffi = True
        except ImportError:
            self._http = requests.Session()

        # Only set UA for plain requests — curl_cffi uses chrome124's native UA so
        # TLS fingerprint and User-Agent stay consistent (CF checks both).
        if not _use_cffi and flare_session.last_user_agent:
            self._http.headers.update({"User-Agent": flare_session.last_user_agent})
        if not _use_cffi and proxy_url:
            self._http.proxies.update({"http": proxy_url, "https": proxy_url})

        for cookie in flare_session.last_cookies:
            cookie_kwargs: dict[str, str] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
            }
            if cookie.domain:
                cookie_kwargs["domain"] = cookie.domain
            self._http.cookies.set(**cookie_kwargs)

    def close(self) -> None:
        self._http.close()

    def post_form(
        self,
        url: str,
        body: str,
        headers: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> requests.Response:
        merged_headers = dict(headers)
        user_agent = str(self._http.headers.get("User-Agent", "") or "").strip()
        if user_agent:
            merged_headers.setdefault("User-Agent", user_agent)
        return self._http.post(
            url,
            data=body,
            headers=merged_headers,
            timeout=timeout_seconds,
            verify=False,
        )


def build_trigger_request(
    available_by_date: dict[str, list[dict[str, Any]]],
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> AvailabilityTriggerRequest:
    availabilities: list[dict[str, Any]] = []

    for date_str in sorted(available_by_date):
        for slot in sorted(
            available_by_date[date_str],
            key=lambda item: str(item.get("time", "")),
        ):
            time_value = str(slot.get("time", "")).strip()
            quantity = _coerce_slot_quantity(slot.get("totalAvailable", 0))
            if not time_value or quantity <= 0:
                continue
            availabilities.append(
                {
                    "date": normalize_trigger_date(date_str),
                    "time": time_value,
                    "quantity": quantity,
                }
            )

    return AvailabilityTriggerRequest(
        source=source,
        metadata=dict(metadata or {}),
        availabilities=availabilities,
    )


def _proxy_display(proxy_value: str) -> str:
    return proxy_display(proxy_value)


def format_available_slots(slots: list[dict[str, Any]]) -> str:
    formatted: list[str] = []
    for slot in sorted(slots, key=lambda item: str(item.get("time", ""))):
        time_value = str(slot.get("time", "")).strip()
        if not time_value:
            continue
        quantity = _coerce_slot_quantity(slot.get("totalAvailable", 0))
        class_name = str(slot.get("class", "")).strip()
        part = f"{time_value} (qty={quantity}"
        if class_name:
            part += f", class={class_name}"
        part += ")"
        formatted.append(part)
    return ", ".join(formatted)


class AvailabilityTriggerClient:
    def __init__(self, settings: AvailabilityCheckerSettings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def trigger(self, request: AvailabilityTriggerRequest) -> dict[str, Any]:
        session = await self._get_session()
        headers = {"Content-Type": "application/json"}
        if self.settings.master_api_key:
            headers["X-API-Key"] = self.settings.master_api_key

        async with session.post(
            f"{self.settings.master_url}/availability/trigger",
            json=request.model_dump(mode="json"),
            headers=headers,
        ) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Availability trigger API {response.status}: {body[:500]}"
                )
            return await response.json(content_type=None)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session


class AvailabilityChecker:
    def __init__(self, settings: AvailabilityCheckerSettings) -> None:
        self.settings = settings
        self.client = AvailabilityTriggerClient(settings)
        self.iproyal_settings = IPRoyalProxySettings.from_env()
        self.all_proxies = (
            []
            if self.iproyal_settings.enabled
            else load_proxies_from_file(PROXIES_FILE)
        )
        self.flaresolverr_urls = get_flaresolverr_urls()
        self._flare_url_index = 0
        self._validated_proxy = ""

        if not self.flaresolverr_urls:
            raise RuntimeError("No FlareSolverr URLs are available for availability checks")

    async def close(self) -> None:
        await self.client.close()

    async def run_forever(self) -> None:
        poll_interval = max(self.settings.poll_interval_seconds, 0.0)
        while True:
            try:
                await self.run_once()
            except AvailabilityBackoffError as exc:
                logger.warning("%s", exc)
            except Exception:
                logger.exception("Availability checker cycle failed")

            if poll_interval <= 0:
                continue

            logger.info("Sleeping %.1fs before next availability scan", poll_interval)
            await asyncio.sleep(poll_interval)

    async def run_once(self) -> dict[str, Any]:
        cycle_started_at = datetime.now().astimezone()
        report: dict[str, Any] = {
            "source": self.settings.source,
            "cycle_started_at": cycle_started_at.isoformat(timespec="seconds"),
            "status": "started",
        }
        logger.info("Starting availability checker cycle")
        try:
            request = await self._scan_availability()
            total_dates = int(request.metadata.get("scanned_dates", 0))
            total_slots = len(request.availabilities)
            report["request"] = request.model_dump(mode="json")
            report["summary"] = {
                "scanned_dates": total_dates,
                "available_dates": int(request.metadata.get("available_dates", 0)),
                "available_slots": total_slots,
            }

            if not request.availabilities:
                logger.info(
                    "No availability found across %d scanned date(s); skipping trigger",
                    total_dates,
                )
                result = {
                    "matched_tasks": 0,
                    "normalized_availabilities": [],
                    "updated_pending_tasks": 0,
                }
                report["status"] = "no-availability"
                report["trigger_result"] = result
                report_path = self._write_cycle_report(cycle_started_at, report)
                logger.info("Availability cycle report written to %s", report_path)
                return {**result, "report_file": str(report_path)}

            logger.info(
                "Trigger request payload:\n%s",
                json.dumps(
                    request.model_dump(mode="json"),
                    indent=2,
                    sort_keys=True,
                ),
            )
            require_trigger = getattr(self.settings, "require_availability_trigger", True)
            if not require_trigger:
                logger.info("require_availability_trigger=false; skipping HTTP trigger")
                report["status"] = "availability-found-no-trigger"
                report["trigger_result"] = {"matched_tasks": 0, "normalized_availabilities": [], "updated_pending_tasks": 0}
                report_path = self._write_cycle_report(cycle_started_at, report)
                logger.info("Availability cycle report written to %s", report_path)
                return {**report["trigger_result"], "report_file": str(report_path)}

            result = await self.client.trigger(request)
            logger.info(
                "Triggered %d availability slot(s) across %d date(s); matched_tasks=%s updated_pending_tasks=%s",
                total_slots,
                total_dates,
                result.get("matched_tasks", 0),
                result.get("updated_pending_tasks", 0),
            )
            report["status"] = "triggered"
            report["trigger_result"] = result
            report_path = self._write_cycle_report(cycle_started_at, report)
            logger.info("Availability cycle report written to %s", report_path)
            return {**result, "report_file": str(report_path)}
        except Exception as exc:
            report["status"] = "error"
            report["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            try:
                report_path = self._write_cycle_report(cycle_started_at, report)
                logger.info("Availability cycle report written to %s", report_path)
            except Exception:
                logger.exception("Failed to write availability cycle report")
            raise

    async def _scan_availability(self) -> AvailabilityTriggerRequest:
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        previous_proxy = ""
        # Force fresh proxy selection on every scan cycle so each cycle uses a different
        # exit IP from the pool. Without this, _validated_proxy is reused indefinitely.
        self._validated_proxy = ""

        for attempt in range(1, TIMESLOTS_403_RETRY_ATTEMPTS + 1):
            flare_url = self._next_flaresolverr_url()
            upstream_proxy = await self._get_upstream_proxy(
                force_new=attempt > 1,
                exclude={previous_proxy} if previous_proxy else None,
            )
            try:
                return await self._scan_availability_once(
                    flare_url=flare_url,
                    upstream_proxy=upstream_proxy,
                    checked_at=checked_at,
                )
            except TimeslotsForbiddenError as exc:
                previous_proxy = upstream_proxy
                if self.all_proxies or self.iproyal_settings.enabled:
                    self._validated_proxy = ""
                if attempt < TIMESLOTS_403_RETRY_ATTEMPTS:
                    logger.warning(
                        "Timeslots request returned HTTP 403 for %s on scan attempt %d/%d; "
                        "destroying the current session and retrying with a new proxy/session",
                        exc.date_str,
                        attempt,
                        TIMESLOTS_403_RETRY_ATTEMPTS,
                    )
                    continue
                raise AvailabilityBackoffError(
                    "Timeslots request returned HTTP 403 for "
                    f"{exc.date_str}; retried with a new proxy/session and still failed, "
                    "so aborting the current availability cycle until the next poll interval"
                ) from exc
            except Exception:
                if self.all_proxies or self.iproyal_settings.enabled:
                    self._validated_proxy = ""
                raise

        raise AvailabilityBackoffError(
            "Availability checker exhausted retry attempts without completing a scan"
        )

    def _load_prewarm_session(self, flare_url: str) -> str:
        prewarm_file = Path("/opt/selenium_bot/prewarm_sessions.json")
        if not prewarm_file.exists():
            return ""
        try:
            sessions: dict[str, str] = json.loads(prewarm_file.read_text())
            sid = sessions.pop(flare_url, "")
            prewarm_file.write_text(json.dumps(sessions))
            return sid
        except Exception:
            return ""

    async def _scan_availability_once(
        self,
        *,
        flare_url: str,
        upstream_proxy: str,
        checked_at: str,
    ) -> AvailabilityTriggerRequest:
        prewarm_sid = self._load_prewarm_session(flare_url)
        session = FlareSession(
            flaresolverr_url=flare_url,
            upstream_proxy=upstream_proxy,
            instance_id=0,
            session_id=prewarm_sid or None,
        )

        logger.info(
            "Scanning availability via %s%s%s",
            flare_url,
            f" using proxy {_proxy_display(upstream_proxy)}" if upstream_proxy else "",
            f" [pre-warmed session {prewarm_sid}]" if prewarm_sid else "",
        )

        try:
            await session.create()
            await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)

            initial_html = session.last_html or ""

            def _is_blocked(html: str) -> bool:
                # Form tokens missing → page didn't load (CF/DataDome/other block)
                if "csrf_name" not in html or "token_tickets" not in html:
                    return True
                # Active DataDome block markers (NOT just the tracking script being present)
                return (
                    "Verification Required" in html
                    or "Slide right to secure your access" in html
                    or "Access is temporarily restricted" in html
                    or 'id="cmsg"' in html
                    or "var dd=" in html
                )

            if _is_blocked(initial_html):
                logger.info("Page blocked on initial tickets load. Attempting DataDome solve...")
                solved = await solve_datadome_in_session_if_needed(session, RESERVATION_URL, upstream_proxy)
                if solved:
                    initial_html = session.last_html or ""

                if _is_blocked(initial_html):
                    raise RuntimeError(
                        "FlareSolverr reached /tickets but Notre-Dame returned DataDome/protection page; "
                        "tickets form/tokens are missing"
                    )

            home_page = HomePageSession(session, 0)
            direct_session = await self._reach_calendar_page(session, home_page)
            available_dates = extract_available_dates_from_calendar_html(
                session.last_html or ""
            )

            logger.info(
                "Found %d clickable date(s) in the calendar DOM",
                len(available_dates),
            )

            available_by_date: dict[str, list[dict[str, Any]]] = {}
            for date_str in available_dates:
                try:
                    payload = await self._fetch_timeslots_payload(
                        session,
                        session.last_html or "",
                        date_str,
                    )
                    slots = extract_available_slots_from_response(payload)
                except Exception as exc:
                    if isinstance(exc, TimeslotsForbiddenError):
                        raise
                    logger.warning("Failed to fetch timeslots for %s: %s", date_str, exc)
                    continue

                if slots:
                    available_by_date[date_str] = slots
                    logger.info(
                        "Availability found for %s: %s",
                        date_str,
                        format_available_slots(slots),
                    )

            metadata: dict[str, Any] = {
                "checked_at": checked_at,
                "scanned_dates": len(available_dates),
                "scanned_date_values": available_dates,
                "available_dates": len(available_by_date),
                "scan_start_date": available_dates[0] if available_dates else "",
                "scan_end_date": available_dates[-1] if available_dates else "",
                "flaresolverr_url": flare_url,
            }
            if upstream_proxy:
                metadata["upstream_proxy"] = _proxy_display(upstream_proxy)

            return build_trigger_request(
                available_by_date,
                source=self.settings.source,
                metadata=metadata,
            )
        finally:
            if "direct_session" in locals():
                direct_session.close()
            await session.close()

    async def _get_upstream_proxy(
        self,
        *,
        force_new: bool = False,
        exclude: set[str] | None = None,
    ) -> str:
        if self.iproyal_settings.enabled:
            return await acquire_warmed_iproyal_proxy(
                self.iproyal_settings,
                warmup=check_proxy,
                logger=logger,
                context="Availability checker",
            )

        if not self.all_proxies:
            return ""
        excluded = {proxy for proxy in (exclude or set()) if proxy}
        if self._validated_proxy and not force_new and self._validated_proxy not in excluded:
            return self._validated_proxy

        candidate_proxies = [proxy for proxy in self.all_proxies if proxy not in excluded]
        fallback_used = False
        if not candidate_proxies:
            candidate_proxies = list(self.all_proxies)
            fallback_used = True

        try:
            validated = await get_validated_proxies(
                needed=1,
                all_proxies=candidate_proxies,
                concurrency=self.settings.proxy_validation_concurrency,
            )
        except RuntimeError:
            if excluded and not fallback_used:
                fallback_used = True
                candidate_proxies = list(self.all_proxies)
                validated = await get_validated_proxies(
                    needed=1,
                    all_proxies=candidate_proxies,
                    concurrency=self.settings.proxy_validation_concurrency,
                )
            else:
                raise

        self._validated_proxy = validated[0]
        if excluded and self._validated_proxy in excluded and fallback_used:
            logger.warning(
                "Could not validate a different proxy for availability retry; reusing %s",
                _proxy_display(self._validated_proxy),
            )
        logger.info("Validated upstream proxy %s", _proxy_display(self._validated_proxy))
        return self._validated_proxy

    def _next_flaresolverr_url(self) -> str:
        url = self.flaresolverr_urls[self._flare_url_index % len(self.flaresolverr_urls)]
        self._flare_url_index += 1
        return url

    async def _reach_calendar_page(
        self,
        session: FlareSession,
        home_page: HomePageSession,
    ) -> ReservationDirectSession:
        for attempt in range(1, CALENDAR_ENTRY_MAX_ATTEMPTS + 1):
            await home_page.wait_for_load()
            await self._submit_ticket_selection(
                session,
                DISCOVERY_TICKET_COUNT,
            )

            if is_calendar_page_html(session.last_html or ""):
                logger.info(
                    "Calendar page reached on attempt %d/%d",
                    attempt,
                    CALENDAR_ENTRY_MAX_ATTEMPTS,
                )
                return ReservationDirectSession(session)

            if is_datadome_or_protection_page(session.last_html or ""):
                logger.info("DataDome protection detected after ticket submit. Attempting to solve...")
                solved = await solve_datadome_in_session_if_needed(
                    session,
                    f"{RESERVATION_URL.rsplit('/', 1)[0]}/date",
                    session.upstream_proxy,
                )
                if solved and is_calendar_page_html(session.last_html or ""):
                    logger.info("Calendar page reached after solving DataDome!")
                    return ReservationDirectSession(session)

            preview = (session.last_html or "").replace("\n", " ").strip()[:200]
            logger.warning(
                "Calendar page not reached after ticket submit (attempt %d/%d). "
                "status=%s url=%s body=%s",
                attempt,
                CALENDAR_ENTRY_MAX_ATTEMPTS,
                session.last_status,
                session.last_url,
                preview,
            )
            if attempt < CALENDAR_ENTRY_MAX_ATTEMPTS:
                await session.get(RESERVATION_URL, timeout=TIMEOUT_NAVIGATION)

        raise RuntimeError(
            "Failed to reach the calendar page after ticket submission retries"
        )

    async def _submit_ticket_selection(
        self,
        flare_session: FlareSession,
        ticket_count: int,
    ) -> None:
        soup = parse_html(flare_session.last_html or "")
        csrf_name = str(
            (soup.select_one('input[name="csrf_name"]') or {}).get("value", "")  # type: ignore[union-attr]
        )
        csrf_value = str(
            (soup.select_one('input[name="csrf_value"]') or {}).get("value", "")  # type: ignore[union-attr]
        )
        token_tickets = str(
            (soup.select_one('input[name="token_tickets"]') or {}).get("value", "")  # type: ignore[union-attr]
        )
        ticket_field_name = str(
            (soup.select_one('select[name^="tickets["]') or {}).get(  # type: ignore[union-attr]
                "name",
                "tickets[411622]",
            )
        )

        if not csrf_name or not csrf_value or not token_tickets:
            raise RuntimeError("Tickets page did not expose the expected submit tokens")

        request_body = urlencode(
            {
                "csrf_name": csrf_name,
                "csrf_value": csrf_value,
                "token_tickets": token_tickets,
                ticket_field_name: str(ticket_count),
            }
        )
        await flare_session.post(
            f"{flare_session.last_url.rsplit('/', 1)[0]}/date",
            post_data=request_body,
            timeout=TIMEOUT_FORM_POST,
            headers=build_navigation_post_headers(
                flare_session.last_url or RESERVATION_URL
            ),
        )

    async def _fetch_timeslots_payload(
        self,
        flare_session: FlareSession,
        calendar_html: str,
        date_str: str,
    ) -> dict[str, Any]:
        product_id = _extract_ticket_product_id(calendar_html)
        request_body = urlencode(
            {
                "tag": "notredame",
                "eventId": "1",
                "productEventId": "",
                "ticketDate": date_str,
                "ticketNumber": str(DISCOVERY_TICKET_COUNT),
                f"ticketNumbers[{product_id}]": str(DISCOVERY_TICKET_COUNT),
                "timeslotsGroup": "",
                "streetname": "reservationindividuelle",
            }
        )
        # Route through FlareSolverr's Chrome so CF WAF can't fingerprint Python HTTP.
        # Chrome navigates to TIMESLOTS_URL and shows JSON in its viewer; we extract
        # the raw JSON from the <pre> tag in the rendered page.
        headers = build_ajax_post_headers(f"{RESERVATION_URL.rsplit('/', 1)[0]}/date")
        await flare_session.post(
            TIMESLOTS_URL,
            post_data=request_body,
            timeout=TIMEOUT_FORM_POST,
            headers=headers,
        )
        html = flare_session.last_html or ""
        # Chrome JSON viewer wraps content in <pre>; plain JSON responses also work.
        soup = parse_html(html)
        pre = soup.find("pre")
        raw_json = pre.get_text(strip=True) if pre else html.strip()
        # CF block check
        if "Attention Required" in html or "Just a moment" in html:
            logger.debug("Timeslots CF block body: %s", html[:300])
            raise TimeslotsForbiddenError(date_str)
        try:
            return json.loads(raw_json)
        except ValueError as exc:
            raise RuntimeError(
                f"Timeslots response was not JSON: {raw_json[:300]}"
            ) from exc

    def _write_cycle_report(
        self,
        cycle_started_at: datetime,
        report: dict[str, Any],
    ) -> str:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.settings.output_dir / build_cycle_report_filename(
            cycle_started_at
        )
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return str(report_path)


async def async_main(*, run_once: bool = False) -> None:
    load_dotenv()
    setup_logging()
    settings = AvailabilityCheckerSettings.from_env()
    checker = AvailabilityChecker(settings)
    try:
        if run_once:
            await checker.run_once()
            return
        await checker.run_forever()
    finally:
        await checker.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuously scan Notre-Dame availability and trigger the master API",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single availability scan instead of looping forever",
    )
    args = parser.parse_args()
    asyncio.run(async_main(run_once=args.once))


if __name__ == "__main__":
    main()
