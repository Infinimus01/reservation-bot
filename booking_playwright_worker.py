"""
booking_playwright_worker.py — Production Playwright booking engine.

Drop-in replacement for flare_bot.run_instance() — no FlareSolverr needed.
Uses real Chrome with persistent browser profile + residential proxy.

Auto-generates personal details (name/phone/zip/country) when not supplied.
Fetches a real email via alias_manager (burner/faker/simplelogin/addy).

Interface (matches flare_bot.run_instance exactly):
    from booking_playwright_worker import run_instance_playwright
    await run_instance_playwright(user_details, instance_id=0, status_callback=cb)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Any, Callable

import aiohttp
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

from alias_manager import create_alias, _create_faker_email
from util import UserDetails

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGS_DIR = Path(__file__).resolve().parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("playwright_worker")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://resa.notredamedeparis.fr"
RESERVATION_URL = f"{BASE_URL}/en/reservationindividuelle/tickets"
DEFAULT_PAYMENT_TURNSTILE_SITEKEY = "0x4AAAAAAA1IAg9Oedxa-RnI"

# Browser profile base dir — each worker instance gets its own subdirectory
BROWSER_PROFILE_BASE = Path(
    os.getenv("WORKER_BROWSER_PROFILE_BASE_DIR", "browser_profiles")
).resolve()

# Timeouts (milliseconds for Playwright, seconds for Python sleeps)
NAV_TIMEOUT_MS = 90_000       # page navigations
FORM_TIMEOUT_MS = 90_000      # form submit waits
SETTLE_MS = 5_000             # pause after each navigation

# Waiting room
WAITING_ROOM_KEYWORDS = [
    "waiting room", "salle d'attente", "patienter",
    "forte affluence", "queue", "veuillez patienter",
]
WAITING_ROOM_POLL_S = int(os.getenv("WAITING_ROOM_POLL_INTERVAL_SECONDS", "15"))
WAITING_ROOM_MAX_S = int(os.getenv("WAITING_ROOM_MAX_WAIT_SECONDS", "600"))

# Block / site protection keywords
DATADOME_MARKERS = [
    "verification required", "slide right to secure",
    "just a moment", "checking your browser", "enable javascript and cookies",
    "captcha-delivery.com",  # DataDome captcha page URL
]
# Stricter markers only present on the actual challenge page (not on normal pages)
DATADOME_CHALLENGE_MARKERS = [
    "captcha-delivery.com",
    "slide right to secure",
    "verification required",
]
ORDER_LIMIT_MARKERS = [
    "maximum amount of orders", "orderlimitreached", "order limit reached",
]
SLOT_FULL_MARKERS = [
    "slot capacity reached", "fully booked", "no longer available",
    "no availability",
]
CONFIRMATION_MARKERS = [
    "reservation is confirmed", "booking is confirmed",
    "thank you for your reservation", "your reservation",
    "thank-you", "orderhash", "thank",
]


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class PlaywrightBookingError(RuntimeError):
    """Base class — all booking errors inherit from this."""


class DataDomeBlockError(PlaywrightBookingError):
    """DataDome or Cloudflare JS challenge blocked the request.
    Worker will retry with a fresh proxy."""


class WaitingRoomTimeoutError(PlaywrightBookingError):
    """Spent too long in the Notre-Dame waiting room."""


class SlotFullError(PlaywrightBookingError):
    """Requested slot is unavailable or has insufficient capacity."""


class OrderLimitError(PlaywrightBookingError):
    """Site-enforced per-user/IP order cap reached — do NOT retry."""


class TurnstileError(PlaywrightBookingError):
    """CapSolver failed to solve the Turnstile CAPTCHA."""


class CSRFMissingError(PlaywrightBookingError):
    """CSRF token absent from page — session may be broken."""


class ConfirmationError(PlaywrightBookingError):
    """Booking submitted but confirmation page not detected."""


# ---------------------------------------------------------------------------
# Personal-details generator  (deterministic — same task_id → same details)
# ---------------------------------------------------------------------------

_FIRST_NAMES = (
    "James", "Emma", "Noah", "Olivia", "Liam", "Ava",
    "Mason", "Sophia", "Ethan", "Grace", "Henry", "Chloe",
    "Lucas", "Mia", "Logan", "Amelia", "Aiden", "Harper",
    "Jackson", "Ella", "Sebastian", "Scarlett", "Carter", "Victoria",
)
_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
    "Miller", "Davis", "Wilson", "Taylor", "Anderson", "Clark",
    "Carter", "Lewis", "Walker", "Hall", "Young", "Allen",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
)
_US_PROFILES = (
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


def _stable_int(*parts: object) -> int:
    raw = "|".join(str(p) for p in parts).encode()
    return int(hashlib.sha1(raw).hexdigest()[:12], 16)


def _stable_choice(options: tuple, *parts: object) -> str:
    return options[_stable_int(*parts) % len(options)]


def _stable_digits(length: int, *parts: object) -> str:
    seed = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(seed.encode()).hexdigest()
    digits = ""
    while len(digits) < length:
        digits += "".join(c for c in digest if c.isdigit())
        digest = hashlib.sha1(digest.encode()).hexdigest()
    return digits[:length]


def generate_personal_details(identity_key: str) -> dict[str, str]:
    """Return stable fake US personal details seeded by identity_key."""
    first = _stable_choice(_FIRST_NAMES, identity_key, "first")
    last = _stable_choice(_LAST_NAMES, identity_key, "last")
    profile = _US_PROFILES[_stable_int(identity_key, "profile") % len(_US_PROFILES)]
    area = _stable_choice(profile["area_codes"], identity_key, "area")
    exch_first = str(2 + _stable_int(identity_key, "exch") % 8)
    phone = f"{area}{exch_first}{_stable_digits(6, identity_key, 'phone')}"
    return {
        "firstName": first,
        "lastName": last,
        "phone": phone,
        "zip": profile["zip"],
        "country": "United States Of America",
    }


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

def proxy_from_line(line: str) -> dict | None:
    """Parse 'host:port:user:pass' into a Playwright proxy dict."""
    if not line:
        return None
    parts = line.split(":", 3)
    if len(parts) < 4:
        logger.warning("proxy_from_line: bad format '%s'", line[:40])
        return None
    host, port, user, password = parts
    return {"server": f"http://{host}:{port}", "username": user, "password": password}


def proxy_display(line: str) -> str:
    parts = line.split(":")
    return f"{parts[0]}:{parts[1]}:{parts[2]}:***" if len(parts) >= 4 else line


# ---------------------------------------------------------------------------
# HTML-state detectors
# ---------------------------------------------------------------------------

def _contains(html: str, markers: list[str]) -> bool:
    lowered = html.lower()
    return any(m in lowered for m in markers)


def _is_datadome(html: str) -> bool:
    return _contains(html, DATADOME_MARKERS)


def _is_datadome_cookie_refresh(html: str) -> str | None:
    """Detect DataDome JSON cookie-refresh: {"status":200,"cookie":"datadome=..."}.
    Returns the cookie value (without 'datadome=' prefix) if found, else None."""
    import re
    m = re.search(r'"cookie"\s*:\s*"(datadome=[^"]+)"', html)
    if m:
        return m.group(1)[len("datadome="):]
    return None


async def _inject_datadome_cookie(page: Page, cookie_value: str) -> None:
    """Inject a datadome cookie value into the page via JS (bypasses Playwright validation)."""
    await page.evaluate(
        "v => { document.cookie = 'datadome=' + v + '; path=/; max-age=86400'; }",
        cookie_value,
    )


async def _solve_datadome_2captcha(
    website_url: str,
    captcha_url: str,
    user_agent: str,
    proxy_line: str,
    log: Callable[[str], None],
) -> str | None:
    """Submit DataDomeSliderTask to 2captcha and return solved cookie value."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY") or os.getenv("TWO_CAPTCHA_API_KEY")
    if not api_key:
        log("2captcha API key not set — cannot solve DataDome")
        return None
    if not proxy_line:
        log("Proxy required for DataDome solving — skipping")
        return None

    parts = proxy_line.split(":")
    if len(parts) < 2:
        log(f"Invalid proxy format for 2captcha: {proxy_line[:30]}")
        return None

    host, port = parts[0], parts[1]
    task: dict[str, Any] = {
        "type": "DataDomeSliderTask",
        "websiteURL": website_url,
        "captchaUrl": captcha_url,
        "userAgent": user_agent,
        "proxyType": "http",
        "proxyAddress": host,
        "proxyPort": int(port),
    }
    if len(parts) >= 4:
        task["proxyLogin"] = parts[2]
        task["proxyPassword"] = parts[3]

    create_url = "https://api.2captcha.com/createTask"
    result_url = "https://api.2captcha.com/getTaskResult"

    log(f"Submitting DataDomeSliderTask to 2captcha for {website_url[:60]}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(create_url, json={"clientKey": api_key, "task": task}) as resp:
                if resp.status != 200:
                    log(f"2captcha createTask HTTP {resp.status}")
                    return None
                data = await resp.json()
            if data.get("errorId", 0) != 0:
                log(f"2captcha createTask error: {data.get('errorDescription')}")
                return None
            task_id = data.get("taskId")
            if not task_id:
                log("2captcha did not return taskId")
                return None
            log(f"2captcha task {task_id} created — polling…")
            for _ in range(60):
                await asyncio.sleep(3)
                async with session.post(result_url, json={"clientKey": api_key, "taskId": task_id}) as resp:
                    if resp.status != 200:
                        continue
                    res = await resp.json()
                if res.get("errorId", 0) != 0:
                    log(f"2captcha poll error: {res.get('errorDescription')}")
                    return None
                if res.get("status") == "ready":
                    cookie_str = res.get("solution", {}).get("cookie")
                    log("2captcha DataDome solved successfully")
                    return cookie_str
            log("2captcha solving timed out after 180s")
        except Exception as exc:
            log(f"2captcha exception: {exc}")
    return None


async def _solve_datadome_on_page(page: Page, proxy_line: str, log: Callable[[str], None]) -> bool:
    """Detect and solve DataDome on the current page. Returns True if solved."""
    import re
    html = await page.content()
    current_url = page.url

    # Case 1: JSON cookie-refresh response
    refresh_value = _is_datadome_cookie_refresh(html)
    if refresh_value:
        log("DataDome cookie-refresh detected — injecting and reloading")
        await _inject_datadome_cookie(page, refresh_value)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(10_000)
        except Exception as exc:
            log(f"Reload after cookie-refresh failed: {exc}")
        return True

    # Case 2: Full DataDome challenge page — must have the captcha-delivery URL
    captcha_url_match = re.search(r'(https://geo\.captcha-delivery\.com/captcha/[^"\'<>\s]+)', html)
    if not captcha_url_match:
        log("DataDome markers matched but no captcha-delivery URL — may be a false positive, skipping")
        return False

    captcha_url = captcha_url_match.group(1)
    user_agent = await page.evaluate("navigator.userAgent")
    log(f"DataDome challenge detected — submitting to 2captcha")

    solved = await _solve_datadome_2captcha(
        website_url=current_url,
        captcha_url=captcha_url,
        user_agent=user_agent,
        proxy_line=proxy_line,
        log=log,
    )
    if not solved:
        log("DataDome 2captcha solve failed")
        return False

    cookie_value = solved
    if cookie_value.lower().startswith("datadome="):
        cookie_value = cookie_value[len("datadome="):]

    log(f"DataDome solved — injecting cookie and reloading")
    await _inject_datadome_cookie(page, cookie_value)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(10_000)
    except Exception as exc:
        log(f"Reload after 2captcha inject failed: {exc}")
    return True


def _is_waiting_room(html: str) -> bool:
    return _contains(html, WAITING_ROOM_KEYWORDS)


def _is_order_limit(html: str) -> bool:
    return _contains(html, ORDER_LIMIT_MARKERS)


def _is_slot_full(html: str) -> bool:
    return _contains(html, SLOT_FULL_MARKERS)


def _is_confirmed(url: str, html: str) -> bool:
    lowered = html.lower()
    return (
        "thank-you" in url.lower()
        or "orderhash" in url.lower()
        or any(m in lowered for m in CONFIRMATION_MARKERS)
    )


# ---------------------------------------------------------------------------
# CapSolver — Turnstile solver
# ---------------------------------------------------------------------------

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"


async def solve_turnstile(
    page_url: str,
    site_key: str,
    log: Callable[[str], None],
) -> str:
    """Solve a Cloudflare Turnstile via CapSolver. Returns the token string."""
    api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not api_key:
        raise TurnstileError("CAPSOLVER_API_KEY is not set")

    payload = {
        "clientKey": api_key,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        },
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.post(CAPSOLVER_CREATE, json=payload) as resp:
            create_data = await resp.json(content_type=None)

        if create_data.get("errorId", 0) != 0:
            raise TurnstileError(
                f"CapSolver createTask error: {create_data.get('errorDescription')}"
            )
        task_id = create_data.get("taskId", "")
        if not task_id:
            raise TurnstileError("CapSolver returned no taskId")

        log(f"CapSolver task created: {task_id}")

        for _ in range(90):           # poll up to 3 minutes
            await asyncio.sleep(2)
            async with http.post(
                CAPSOLVER_RESULT,
                json={"clientKey": api_key, "taskId": task_id},
            ) as resp:
                result = await resp.json(content_type=None)

            status = result.get("status", "")
            if status == "ready":
                token = result.get("solution", {}).get("token", "")
                if not token:
                    raise TurnstileError("CapSolver ready but token is empty")
                log(f"Turnstile solved: {token[:22]}…")
                return token
            if status == "failed":
                raise TurnstileError(
                    f"CapSolver task failed: {result.get('errorDescription')}"
                )

    raise TurnstileError("CapSolver polling timed out (180 s)")


# ---------------------------------------------------------------------------
# Low-level page utilities
# ---------------------------------------------------------------------------

async def _post_form(page: Page, url: str, fields: dict[str, str]) -> None:
    """Submit a hidden HTML form via JS and wait for navigation."""
    await page.evaluate(
        """({url, fields}) => {
            const f = document.createElement('form');
            f.method = 'POST'; f.action = url; f.style.display = 'none';
            for (const [n, v] of Object.entries(fields)) {
                const i = document.createElement('input');
                i.type = 'hidden'; i.name = n; i.value = v;
                f.appendChild(i);
            }
            document.body.appendChild(f); f.submit();
        }""",
        {"url": url, "fields": fields},
    )
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=FORM_TIMEOUT_MS)
    except Exception:
        await page.wait_for_timeout(8_000)


async def _get_csrf(page: Page) -> tuple[str, str]:
    result = await page.evaluate("""
    () => ({
        name:  document.querySelector('input[name="csrf_name"]')?.value  || "",
        value: document.querySelector('input[name="csrf_value"]')?.value || ""
    })
    """)
    return result["name"], result["value"]


async def _handle_waiting_room(page: Page, log: Callable[[str], None]) -> None:
    """Block until the site exits the waiting room or raise WaitingRoomTimeoutError."""
    html = await page.content()
    if not _is_waiting_room(html):
        return

    log("Waiting room detected — polling…")
    waited = 0
    while waited < WAITING_ROOM_MAX_S:
        await asyncio.sleep(WAITING_ROOM_POLL_S)
        waited += WAITING_ROOM_POLL_S
        try:
            await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        html = await page.content()
        if not _is_waiting_room(html):
            log(f"Left waiting room after {waited}s")
            return
        log(f"Still in waiting room — {waited}/{WAITING_ROOM_MAX_S}s")

    raise WaitingRoomTimeoutError(
        f"Waiting room exceeded {WAITING_ROOM_MAX_S}s — giving up"
    )


# ---------------------------------------------------------------------------
# BookingEngine — orchestrates the 6-step flow
# ---------------------------------------------------------------------------

class BookingEngine:
    """
    Runs the full Notre-Dame ticket booking flow for a single task.

    Steps:
      1. Navigate to tickets page, handle DataDome/waiting-room, submit ticket count
      2. Fetch timeslots, validate seat count, submit calendar (date + time)
      3. Fill personal details (name / email / phone / zip / country)
      4. Submit zero-donation → lands on summary page
      5. Submit summary → lands on final payment page
      6. Check T&C, solve Turnstile via CapSolver, click Complete, verify thank-you
    """

    def __init__(
        self,
        *,
        task_id: str,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        zip_code: str,
        country: str,
        date: str,
        time: str,
        ticket_count: int,
        proxy_line: str,
        profile_dir: Path,
        instance_id: int,
        log: Callable[[str, int], None],
        stage_cb: Callable[[str], None],
    ) -> None:
        self.task_id = task_id
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.phone = phone
        self.zip_code = zip_code
        self.country = country
        self.date = date
        self.time = time
        self.ticket_count = ticket_count
        self.proxy_line = proxy_line
        self.profile_dir = profile_dir
        self.instance_id = instance_id
        self._log = log
        self._stage = stage_cb

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute the full booking. Raises PlaywrightBookingError on failure."""
        proxy = proxy_from_line(self.proxy_line)
        self._log(
            f"Starting booking | {self.first_name} {self.last_name} | "
            f"{self.date} {self.time} ×{self.ticket_count} | "
            f"proxy={proxy_display(self.proxy_line)} | profile={self.profile_dir.name}",
            logging.INFO,
        )

        self.profile_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=True,
                locale="en-US",
                no_viewport=True,
                proxy=proxy,
                args=[
                    "--window-size=1440,900",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                page = await context.new_page()
                await self._run_flow(page)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Private: full flow
    # ------------------------------------------------------------------

    async def _run_flow(self, page: Page) -> None:
        self._stage("STEP 1/6: Tickets Page")
        await self._step1_tickets(page)

        self._stage("STEP 2/6: Calendar Page")
        await self._step2_calendar(page)

        self._stage("STEP 3/6: Personal Details")
        await self._step3_details(page)

        self._stage("STEP 4/6: Donation Page")
        await self._step4_donation(page)

        self._stage("STEP 5/6: Summary Page")
        await self._step5_summary(page)

        self._stage("STEP 6/6: Final Payment")
        await self._step6_complete(page)

        self._log("Booking complete ✅", logging.INFO)

    # ------------------------------------------------------------------
    # Step 1 — Tickets page
    # ------------------------------------------------------------------

    async def _step1_tickets(self, page: Page) -> None:
        self._log(f"Navigating to {RESERVATION_URL}", logging.INFO)
        try:
            await page.goto(RESERVATION_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            raise PlaywrightBookingError(f"Tickets page navigation failed: {exc}") from exc

        await page.wait_for_timeout(10_000)  # 10s — matches lane file; gives CF time to settle

        await self._guard_block_with_solve(page, "tickets page after load")
        await _handle_waiting_room(page, lambda m: self._log(m, logging.WARNING))
        html = await page.content()

        if "csrf_name" not in html:
            raise CSRFMissingError(
                "Tickets page missing CSRF after waiting-room exit — "
                "may need a fresh browser profile"
            )

        # Extract product id from ticket select element
        product_id = await page.evaluate("""
        () => {
            const sel = document.querySelector('select[name^="tickets["]');
            const m = sel?.getAttribute('name')?.match(/tickets\\[(\\d+)\\]/);
            return m ? m[1] : "411622";
        }
        """)
        csrf_name, csrf_value = await _get_csrf(page)
        token_tickets = await page.evaluate(
            "() => document.querySelector('input[name=\"token_tickets\"]')?.value || ''"
        )

        await _post_form(page, f"{BASE_URL}/en/reservationindividuelle/date", {
            "csrf_name": csrf_name,
            "csrf_value": csrf_value,
            "token_tickets": token_tickets,
            f"tickets[{product_id}]": str(self.ticket_count),
            "donation-input": "0",
            "donationCheck": "true",
        })
        await page.wait_for_timeout(8_000)  # 8s — matches lane file

        await self._guard_block_with_solve(page, "after ticket submit")
        self._log(f"Ticket submit OK → {page.url}", logging.INFO)

    # ------------------------------------------------------------------
    # Step 2 — Calendar / timeslot
    # ------------------------------------------------------------------

    async def _step2_calendar(self, page: Page) -> None:
        html = await page.content()
        if "ticketDate" not in html and "csrf_name" not in html:
            raise PlaywrightBookingError(
                f"Calendar page not reached. Current URL: {page.url}"
            )

        # Parse date components for jQuery UI datepicker selectors.
        # data-month is 0-indexed (January=0, June=5).
        try:
            y, m, d = self.date.split("-")
            js_month = str(int(m) - 1)
            js_day = str(int(d))
        except ValueError:
            raise PlaywrightBookingError(f"Invalid date format: {self.date!r}")

        # Click the actual calendar date cell — real UI interaction,
        # which CF treats as legitimate human navigation.
        self._log(
            f"Clicking calendar date {self.date} "
            f"(year={y}, js_month={js_month}, day={js_day})",
            logging.INFO,
        )
        clicked = await page.evaluate(
            """({year, month, day}) => {
                const cells = document.querySelectorAll(
                    'td[data-handler="selectDay"][data-month="' + month +
                    '"][data-year="' + year + '"]'
                );
                for (const cell of cells) {
                    const a = cell.querySelector('a');
                    if (a && a.textContent.trim() === day) {
                        a.click();
                        return 'clicked day ' + day;
                    }
                }
                return null;
            }""",
            {"year": y, "month": js_month, "day": js_day},
        )

        if clicked:
            self._log(f"Calendar date click OK: {clicked}", logging.INFO)
            await page.wait_for_timeout(3_000)   # let any UI update settle
        else:
            self._log(
                f"Calendar date cell not found — will inject ticketDate directly",
                logging.WARNING,
            )

        # Verify CSRF is present before form submission
        csrf_name, csrf_value = await _get_csrf(page)
        if not csrf_name:
            raise CSRFMissingError("Calendar page: CSRF token missing")

        self._log(f"Submitting calendar: {self.date} {self.time}", logging.INFO)

        # Inject ticketDate/ticketTime into the EXISTING page form and submit it.
        # This carries all the hidden fields the server already put on the page
        # (product IDs, ticket counts, etc.) and avoids creating a synthetic form
        # that CF may reject due to missing expected fields.
        inject_result = await page.evaluate(
            """({ticketDate, ticketTime}) => {
                const form = document.querySelector('form') || document.forms[0];
                if (!form) return {ok: false, reason: 'no form on page'};

                const setOrCreate = (name, val) => {
                    let el = form.querySelector('input[name="' + name + '"]');
                    if (!el) {
                        el = document.createElement('input');
                        el.type = 'hidden';
                        el.name = name;
                        form.appendChild(el);
                    }
                    el.value = val;
                };

                setOrCreate('ticketDate', ticketDate);
                setOrCreate('ticketTime', ticketTime);

                const names = Array.from(form.querySelectorAll('input')).map(i => i.name);
                return {ok: true, action: form.action, fields: names};
            }""",
            {"ticketDate": self.date, "ticketTime": self.time},
        )

        form_action = inject_result.get("action", "") if inject_result.get("ok") else ""
        self._log(
            f"Calendar form fields: {inject_result.get('fields')} → action={form_action}",
            logging.INFO,
        )

        if "/personal-details" in form_action:
            # Date click updated the form action — submit the existing form as-is.
            # This carries token_tickets and any other fields the server expects.
            try:
                async with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=FORM_TIMEOUT_MS
                ):
                    await page.locator("form").evaluate("f => f.submit()")
            except Exception as exc:
                self._log(f"Calendar form nav warning: {exc}", logging.WARNING)
                await page.wait_for_timeout(8_000)
        else:
            # Date click didn't update form action (cell not found or click had no effect).
            # Use a synthetic form posted directly to /personal-details — the same approach
            # as booking_browser_lane.py which works reliably in the reference environment.
            self._log(
                f"Form action not /personal-details (got {form_action!r}) — "
                "using _post_form directly to /personal-details",
                logging.WARNING,
            )
            await _post_form(
                page,
                f"{BASE_URL}/en/reservationindividuelle/personal-details",
                {
                    "csrf_name": csrf_name,
                    "csrf_value": csrf_value,
                    "ticketDate": self.date,
                    "ticketTime": self.time,
                },
            )

        await page.wait_for_timeout(10_000)

    # ------------------------------------------------------------------
    # Human-like interaction helpers (anti-bot — mimic a real user)
    # ------------------------------------------------------------------

    async def _human_pause(self, page: Page, lo: int = 120, hi: int = 360) -> None:
        await page.wait_for_timeout(random.randint(lo, hi))

    async def _human_move_to(self, page: Page, locator) -> bool:
        """Move the mouse to a random point inside the element along a natural path."""
        try:
            box = await locator.bounding_box()
            if not box:
                return False
            tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await page.mouse.move(tx, ty, steps=random.randint(8, 20))
            return True
        except Exception:
            return False

    async def _human_type_field(self, page: Page, name: str, value: str) -> str:
        """
        Fill one form field the way a person would: move mouse → click to focus →
        clear → type character-by-character with jittered delays → blur.
        Falls back to JS for hidden fields. Returns 'typed' | 'js' | 'missing'.
        """
        locator = page.locator(f'input[name="{name}"]').first
        try:
            if await locator.count() == 0:
                return "missing"
            visible = await locator.is_visible()
            editable = await locator.is_editable()
        except Exception:
            visible = editable = False

        if visible and editable:
            await self._human_move_to(page, locator)
            await self._human_pause(page, 80, 220)
            try:
                await locator.click()
            except Exception:
                try:
                    await locator.focus()
                except Exception:
                    pass
            await self._human_pause(page, 60, 180)
            try:
                await locator.fill("")  # clear any pre-filled value
            except Exception:
                pass
            for ch in value:
                await page.keyboard.type(ch)
                await page.wait_for_timeout(random.randint(45, 140))
                if random.random() < 0.07:  # occasional "thinking" pause
                    await page.wait_for_timeout(random.randint(220, 520))
            await self._human_pause(page, 120, 300)
            try:
                await locator.evaluate(
                    "el => { el.dispatchEvent(new Event('change', {bubbles:true})); el.blur(); }"
                )
            except Exception:
                pass
            self._log(f"  typed {name} (human)", logging.INFO)
            return "typed"

        # Field present but not visible — set via JS so it is still POSTed
        try:
            await locator.evaluate(
                "(el, v) => { el.value = v;"
                " el.dispatchEvent(new Event('input',{bubbles:true}));"
                " el.dispatchEvent(new Event('change',{bubbles:true})); }",
                value,
            )
            self._log(f"  set {name} (hidden/js)", logging.INFO)
            return "js"
        except Exception:
            return "missing"

    async def _human_select_country(self, page: Page, country: str) -> None:
        sel = page.locator('select[name="country"]').first
        try:
            if await sel.count() == 0:
                return
        except Exception:
            return
        code = await sel.evaluate(
            """(el, wanted) => {
                wanted = (wanted || '').toLowerCase().trim();
                let code = el.value || 'US';
                for (const opt of el.querySelectorAll('option')) {
                    const t = (opt.textContent || '').toLowerCase().trim();
                    if (!t || t === 'choose a country') continue;
                    if (t === wanted || t.includes(wanted) || wanted.includes(t)) {
                        code = opt.value; break;
                    }
                }
                return code;
            }""",
            country,
        )
        await self._human_move_to(page, sel)
        await self._human_pause(page, 120, 320)
        try:
            await sel.select_option(code)
        except Exception:
            try:
                await sel.evaluate(
                    "(el, v) => { el.value = v;"
                    " el.dispatchEvent(new Event('change', {bubbles:true})); }",
                    code,
                )
            except Exception:
                pass
        self._log(f"  selected country={code}", logging.INFO)
        await self._human_pause(page, 150, 360)

    async def _ensure_form_fields(self, page: Page, fields: dict[str, str]) -> None:
        """Guarantee required fields are present/non-empty for the POST (creates hidden ones)."""
        await page.evaluate(
            """(fields) => {
                const form = document.querySelector('form') || document.forms[0];
                if (!form) return;
                for (const [name, val] of Object.entries(fields)) {
                    let el = form.querySelector('[name="' + name + '"]');
                    if (el) {
                        if (!el.value) {
                            el.value = val;
                            el.dispatchEvent(new Event('input',{bubbles:true}));
                            el.dispatchEvent(new Event('change',{bubbles:true}));
                        }
                    } else {
                        el = document.createElement('input');
                        el.type = 'hidden'; el.name = name; el.value = val;
                        form.appendChild(el);
                    }
                }
            }""",
            fields,
        )

    async def _human_submit_form(self, page: Page) -> None:
        """Click a real submit button if present (human), else fall back to form.submit()."""
        btn = None
        for sel in (
            'form button[type="submit"]',
            'form input[type="submit"]',
            'button[type="submit"]',
            'button.btn-primary',
            'form button',
        ):
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    btn = loc
                    break
            except Exception:
                continue
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=FORM_TIMEOUT_MS
            ):
                if btn is not None:
                    await self._human_move_to(page, btn)
                    await self._human_pause(page, 160, 420)
                    await btn.click()
                else:
                    await page.locator("form").evaluate("f => f.submit()")
        except Exception as exc:
            self._log(f"Personal details submit nav warning: {exc}", logging.WARNING)
            await page.wait_for_timeout(10_000)

    # ------------------------------------------------------------------
    # Step 3 — Personal details
    # ------------------------------------------------------------------

    async def _step3_details(self, page: Page) -> None:
        html = await page.content()
        # CF may auto-redirect after a few seconds — retry up to 30s total
        for _wait in (5, 8, 10):
            if "firstName" in html or "emailAddress" in html:
                break
            if _is_datadome(html):
                break  # let _guard_block_with_solve handle it below
            self._log(
                f"Personal details form not yet visible — waiting {_wait}s for CF redirect",
                logging.WARNING,
            )
            await page.wait_for_timeout(_wait * 1_000)
            html = await page.content()

        await self._guard_block_with_solve(page, "personal details page")
        html = await page.content()

        if "firstName" not in html and "emailAddress" not in html:
            self._log(f"Personal details HTML[0:600]: {html[:600]}", logging.WARNING)
            raise PlaywrightBookingError(
                f"Personal details page not reached. URL: {page.url}"
            )

        self._log(
            f"Filling details (human mode): {self.first_name} {self.last_name} | "
            f"email={self.email} | phone={self.phone} | zip={self.zip_code}",
            logging.INFO,
        )

        # Settle + a little idle mouse movement before touching the form,
        # the way a person orients on a new page.
        await self._human_pause(page, 500, 1100)
        try:
            await page.mouse.move(
                random.randint(220, 640), random.randint(160, 420),
                steps=random.randint(8, 16),
            )
        except Exception:
            pass

        # Fill each field one at a time — real mouse move, click, char-by-char typing.
        await self._human_type_field(page, "firstName", self.first_name)
        await self._human_type_field(page, "surname", self.last_name)
        await self._human_type_field(page, "emailAddress", self.email)
        await self._human_type_field(page, "emailAddressConfirm", self.email)

        # Phone: type whichever phone input is visible; guarantee both POST values.
        phone_typed = await self._human_type_field(page, "phoneNumber", self.phone)
        if phone_typed == "missing":
            await self._human_type_field(page, "phone-number", f"+44{self.phone}")

        await self._human_type_field(page, "zipcode", self.zip_code)

        # Country dropdown (human-ish: move → select)
        await self._human_select_country(page, self.country)

        # Guarantee required POST fields exist even if hidden / intl-tel-input.
        await self._ensure_form_fields(
            page,
            {
                "firstName": self.first_name,
                "surname": self.last_name,
                "emailAddress": self.email,
                "emailAddressConfirm": self.email,
                "phoneNumber": self.phone,
                "phone-number": f"+44{self.phone}",
                "zipcode": self.zip_code,
            },
        )

        # Brief review pause, then submit by clicking the real button.
        await self._human_pause(page, 400, 900)
        await self._human_submit_form(page)

        await page.wait_for_timeout(10_000)

        html = await page.content()
        if _is_order_limit(html):
            raise OrderLimitError("Order limit reached after personal details submit")

    # ------------------------------------------------------------------
    # Step 4 — Donation (zero) → summary
    # ------------------------------------------------------------------

    async def _step4_donation(self, page: Page) -> None:
        html = await page.content()
        self._guard_block(html, "donation page")

        if "/payment" not in page.url and "donation" not in html:
            raise PlaywrightBookingError(
                f"Donation page not reached. URL: {page.url}"
            )

        # Uncheck donation and set amount to 0 via JS (same as booking_browser_lane.py)
        result = await page.evaluate("""
        async () => {
            const form = document.forms[0];
            if (!form) return {ok:false, reason:"no form"};
            const csrfName = document.querySelector('input[name="csrf_name"]')?.value || "";
            const csrfValue = document.querySelector('input[name="csrf_value"]')?.value || "";
            if (!csrfName || !csrfValue) return {ok:false, reason:"missing csrf"};
            const check = document.querySelector('input[name="donation-check"]');
            if (check) check.checked = false;
            const hiddenCheck = document.querySelector('input[name="donationCheck"]');
            if (hiddenCheck) hiddenCheck.value = 'true';
            const radios = Array.from(document.querySelectorAll('input[name="donation-input"][type="radio"]'));
            for (const r of radios) r.checked = false;
            const num = document.querySelector('input[name="donation-input"][type="number"]');
            if (num) num.value = '0';
            return {ok: true, action: form.action, csrfName, csrfValue};
        }
        """)
        if not result.get("ok"):
            raise PlaywrightBookingError(f"Donation form prepare failed: {result}")
        self._log(f"Donation form ready: action={result.get('action')}", logging.INFO)

        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            ):
                await page.locator("form").evaluate("(form) => form.submit()")
        except Exception as exc:
            self._log(f"Donation nav warning: {exc}", logging.WARNING)
            await page.wait_for_timeout(12_000)

        await page.wait_for_timeout(5_000)
        self._log(f"Donation submit → {page.url}", logging.INFO)

    # ------------------------------------------------------------------
    # Step 5 — Summary → final /payment
    # ------------------------------------------------------------------

    async def _step5_summary(self, page: Page) -> None:
        html = await page.content()
        self._guard_block(html, "summary page")

        if "csrf_name" not in html:
            raise CSRFMissingError(f"Summary page missing CSRF. URL: {page.url}")

        self._log("Submitting summary → final payment page", logging.INFO)
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            ):
                await page.locator("form").evaluate("f => f.submit()")
        except Exception as exc:
            self._log(f"Summary nav warning: {exc}", logging.WARNING)
            await page.wait_for_timeout(8_000)

        await page.wait_for_timeout(SETTLE_MS)
        self._log(f"Summary submit → {page.url}", logging.INFO)

    # ------------------------------------------------------------------
    # Step 6 — T&C + Turnstile + Complete
    # ------------------------------------------------------------------

    async def _step6_complete(self, page: Page) -> None:
        html = await page.content()
        current_url = page.url

        self._guard_block(html, "final payment page")

        if _is_order_limit(html):
            raise OrderLimitError("Order limit on final payment page")

        if "terms-and-conditions" not in html and "cf-turnstile-response" not in html:
            raise PlaywrightBookingError(
                f"Final payment page has unexpected structure at {current_url}. "
                f"HTML snippet: {html[:400]}"
            )

        # Extract Turnstile sitekey
        site_key = await page.evaluate("""
        () => {
            const el = document.querySelector('.cf-turnstile, [data-sitekey]');
            if (el) return el.getAttribute('data-sitekey') || '';
            const iframe = document.querySelector('iframe[src*="turnstile"]');
            if (iframe) {
                const m = (iframe.src || '').match(/[?&]k=([^&]+)/);
                if (m) return m[1];
            }
            for (const s of document.querySelectorAll('script')) {
                const m = (s.textContent || '').match(/sitekey["\\s:=]+["']([0-9a-zA-Z_-]+)["']/);
                if (m) return m[1];
            }
            return '';
        }
        """)

        if not site_key:
            site_key = DEFAULT_PAYMENT_TURNSTILE_SITEKEY
            self._log(
                f"Turnstile sitekey not found in DOM — using fallback {site_key[:12]}…",
                logging.WARNING,
            )
        else:
            self._log(f"Turnstile sitekey: {site_key[:12]}…", logging.INFO)

        # Solve Turnstile
        self._stage("STEP 6/6: Solving Turnstile")
        token = await solve_turnstile(
            current_url, site_key, lambda m: self._log(m, logging.INFO)
        )

        # Inject T&C + token
        await page.evaluate(
            """(token) => {
                const cb = document.querySelector('input[name="terms-and-conditions"]');
                if (cb) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', {bubbles: true}));
                }
                // Inject into all existing hidden inputs
                document.querySelectorAll('input[name="cf-turnstile-response"]')
                    .forEach(i => { i.value = token; });
                // Also add to any .cf-turnstile widget that might be missing the input
                document.querySelectorAll('.cf-turnstile').forEach(w => {
                    let inp = w.querySelector('input[name="cf-turnstile-response"]');
                    if (!inp) {
                        inp = document.createElement('input');
                        inp.type = 'hidden';
                        inp.name = 'cf-turnstile-response';
                        w.appendChild(inp);
                    }
                    inp.value = token;
                });
            }""",
            token,
        )
        self._log("T&C checked, Turnstile token injected", logging.INFO)

        # Click Complete
        self._stage("STEP 6/6: Clicking Complete")
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            ):
                await page.evaluate("""
                () => {
                    const btn = document.querySelector(
                        'button[type="submit"], input[type="submit"], button.complete, .btn-complete'
                    );
                    if (btn) { btn.click(); return true; }
                    const form = document.forms[0];
                    if (form) { form.submit(); return true; }
                    return false;
                }
                """)
        except Exception as exc:
            self._log(f"Complete nav warning (may be OK): {exc}", logging.WARNING)
            await page.wait_for_timeout(15_000)

        await page.wait_for_timeout(5_000)
        final_url = page.url
        final_html = await page.content()

        self._log(f"Post-Complete URL: {final_url}", logging.INFO)

        if _is_order_limit(final_html):
            raise OrderLimitError("Order limit on thank-you redirect")

        if not _is_confirmed(final_url, final_html):
            self._log(f"Post-Complete HTML[400:900]: {final_html[400:900]}", logging.WARNING)
            raise ConfirmationError(
                f"Confirmation not detected. URL={final_url} | "
                f"html_snippet={final_html[:600]}"
            )

        self._log(f"BOOKING CONFIRMED ✅  {final_url}", logging.INFO)

    # ------------------------------------------------------------------
    # Guard helper — check for blocks on any page
    # ------------------------------------------------------------------

    def _guard_block(self, html: str, context: str) -> None:
        if _is_datadome(html):
            raise DataDomeBlockError(f"DataDome/CF block detected at: {context}")

    async def _guard_block_with_solve(self, page: Page, context: str) -> None:
        """Check for DataDome and attempt to solve it before raising."""
        html = await page.content()
        if not _is_datadome(html):
            return
        self._log(f"DataDome detected at: {context} — attempting 2captcha solve", logging.WARNING)
        solved = await _solve_datadome_on_page(page, self.proxy_line, lambda m: self._log(m, logging.INFO))
        if solved:
            html = await page.content()
            # After solving, check strictly — "datadome" appears on normal pages too.
            # We pass if csrf_name is present (booking form loaded) or no strict challenge markers remain.
            if "csrf_name" in html or not _contains(html, DATADOME_CHALLENGE_MARKERS):
                self._log(f"DataDome cleared at: {context}", logging.INFO)
                return
        raise DataDomeBlockError(f"DataDome/CF block detected at: {context} (solve failed)")


# ---------------------------------------------------------------------------
# run_instance_playwright — drop-in for flare_bot.run_instance
# ---------------------------------------------------------------------------

async def run_instance_playwright(
    user_details: UserDetails,
    instance_id: int = 0,
    instance_status: dict | None = None,
    flaresolverr_url: str | None = None,        # kept for interface compat, unused
    flaresolverr_urls: list[str] | None = None,  # kept for interface compat, unused
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> None:
    """
    Run a single Notre-Dame booking via direct Playwright/Chrome.

    Fires status_callback with:
        {"outcome": "running"|"success"|"failed"|"interrupted",
         "stage": <current step>, "error": <message>}

    Raises DataDomeBlockError so worker_main can classify it as CloudflareBlockException
    and retry with a fresh proxy.
    """
    run_metadata = run_metadata or {}
    worker_id = str(run_metadata.get("worker_id", f"w{instance_id}"))
    task_id = str(run_metadata.get("task_id", user_details.unique_id))
    prefix = f"[W:{worker_id}][T:{task_id}][I:{instance_id}]"

    def _log(msg: str, level: int = logging.INFO) -> None:
        logger.log(level, "%s %s", prefix, msg)

    def _stage(label: str) -> None:
        _log(f"Stage: {label}")
        if status_callback:
            status_callback({"outcome": "running", "stage": label, "error": ""})

    def _finish(outcome: str, stage: str, error: str = "") -> None:
        _log(
            f"outcome={outcome} | stage={stage}"
            + (f" | error={error[:200]}" if error else ""),
            logging.INFO if outcome == "success" else logging.WARNING,
        )
        if status_callback:
            status_callback({"outcome": outcome, "stage": stage, "error": error})

    _stage("Initialising")

    # ── Resolve / auto-generate personal details ──────────────────────
    first = (user_details.firstName or "").strip()
    last = (user_details.lastName or "").strip()
    phone = (user_details.phone or "").strip()
    zip_code = (user_details.zip or "").strip()
    country = (user_details.country or "").strip() or "United States Of America"

    if not all([first, last, phone, zip_code]):
        generated = generate_personal_details(task_id)
        first = first or generated["firstName"]
        last = last or generated["lastName"]
        phone = phone or generated["phone"]
        zip_code = zip_code or generated["zip"]
        country = country or generated["country"]
        _log(f"Auto-generated: {first} {last} | phone={phone} | zip={zip_code}")

    # ── Resolve / generate email — ALWAYS fresh per attempt ──────────
    # Notre-Dame rejects an email once it has been used for any booking (even partial).
    # Using a fresh unique email every attempt prevents "email already used" rejections.
    # We IGNORE the email from the sheet and always generate fresh.
    email_provider = os.getenv("WORKER_EMAIL_PROVIDER", "faker").strip() or "faker"
    _stage("Generating email")
    try:
        email = await create_alias(email_provider, first_name=first, last_name=last)
        _log(f"Email ({email_provider}): {email}")
    except Exception as exc:
        _log(
            f"Email provider '{email_provider}' failed: {exc} — falling back to faker",
            logging.WARNING,
        )
        email = _create_faker_email(first, last)
        _log(f"Faker email: {email}")

    # ── Resolve proxy ─────────────────────────────────────────────────
    proxy_line = (user_details.upstream_proxy or user_details.proxy or "").strip()
    if not proxy_line:
        _finish("failed", "Proxy missing", "No upstream_proxy or proxy on task — cannot book")
        return

    _log(f"Proxy: {proxy_display(proxy_line)}")

    # ── Resolve browser profile dir — stable per task across retries ──
    # Using the same profile on every retry means CF/DataDome cookies accumulate,
    # so each subsequent attempt has a warmer session and is less likely to be blocked.
    profile_base = Path(
        os.getenv("WORKER_BROWSER_PROFILE_BASE_DIR", "browser_profiles")
    ).resolve()
    profile_index = _stable_int(task_id) % 50
    profile_dir = profile_base / f"worker_{profile_index}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Browser profile: {profile_dir} (task-stable index {profile_index})")

    # ── Run booking ───────────────────────────────────────────────────
    engine = BookingEngine(
        task_id=task_id,
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
        zip_code=zip_code,
        country=country,
        date=user_details.date,
        time=user_details.time,
        ticket_count=user_details.ticket_count,
        proxy_line=proxy_line,
        profile_dir=profile_dir,
        instance_id=instance_id,
        log=_log,
        stage_cb=_stage,
    )

    try:
        await engine.run()
        _finish("success", "Completed")

    except OrderLimitError as exc:
        # Non-retryable — site hard limit, don't waste another attempt
        _finish("failed", "Order limit reached", str(exc))

    except SlotFullError as exc:
        # Non-retryable — slot is gone
        _finish("failed", "Slot full or unavailable", str(exc))

    except DataDomeBlockError as exc:
        # Treated as a CF block — worker_main will retry with new proxy
        _finish("failed", "CF/DataDome block", str(exc))
        raise  # re-raise so worker_main catches CloudflareBlockException

    except TurnstileError as exc:
        _finish("failed", "Turnstile solve failed", str(exc))

    except CSRFMissingError as exc:
        _finish("failed", "CSRF error", str(exc))

    except ConfirmationError as exc:
        _finish("failed", "Confirmation not detected", str(exc))

    except WaitingRoomTimeoutError as exc:
        _finish("failed", "Waiting room timeout", str(exc))

    except asyncio.CancelledError:
        _finish("interrupted", "Cancelled", "Task was cancelled externally")
        raise

    except Exception as exc:
        _log(f"Unexpected error: {type(exc).__name__}: {exc}", logging.ERROR)
        _finish("failed", "Unexpected error", f"{type(exc).__name__}: {exc}")
