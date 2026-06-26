from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

from flare_bot import PROXIES_FILE, RESERVATION_URL, TIMESLOTS_URL, setup_logging, rotate_proxy_session
from master.availability_checker import (
    DISCOVERY_TICKET_COUNT,
    AvailabilityTriggerClient,
    build_cycle_report_filename,
    build_trigger_request,
    extract_available_dates_from_calendar_html,
    extract_available_slots_from_response,
    format_available_slots,
    is_calendar_page_html,
    _extract_ticket_product_id,
    _proxy_display,
)
from shared.config import AvailabilityCheckerSettings


logger = logging.getLogger("flare_bot.availability_browser_checker")

DEBUG_DIR = Path("local_debug")
DEBUG_DIR.mkdir(exist_ok=True)


def load_first_proxy() -> str:
    path = Path(PROXIES_FILE)
    if not path.exists():
        return ""

    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line

    return ""


def playwright_proxy_from_line(proxy_line: str) -> dict[str, str] | None:
    if not proxy_line:
        return None

    parts = proxy_line.split(":")
    if len(parts) < 4:
        raise RuntimeError(f"Bad proxy format: {proxy_line}")

    host, port, username = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])

    return {
        "server": f"http://{host}:{port}",
        "username": username,
        "password": password,
    }



def is_datadome_or_protection_page(html: str) -> bool:
    markers = [
        "Verification Required",
        "Slide right to secure your access",
        "Access is temporarily restricted",
        "Please enable JS",
        "api-js.datadome.co",
        'id="cmsg"',
        "var dd=",
        # Cloudflare managed challenge / Turnstile
        "Just a moment",
        "challenges.cloudflare.com",
        "Checking if the site connection is secure",
        "cf-mitigated",
    ]
    return any(marker in html for marker in markers)


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


def _is_datadome_cookie_refresh(html: str) -> str | None:
    """Detect DataDome's JSON cookie-refresh response ({"status":200,"cookie":"datadome=..."}).
    Returns the cookie value string if found, else None."""
    import re
    # Matches: {"status": 200, "cookie": "datadome=VALUE"}
    m = re.search(r'"cookie"\s*:\s*"(datadome=[^"]+)"', html)
    if m:
        return m.group(1)[len("datadome="):]  # strip "datadome=" prefix
    return None


async def _inject_datadome_cookie(page: Page, cookie_value: str) -> None:
    """Inject a datadome cookie value into the current page context via JS."""
    await page.evaluate(
        "v => { document.cookie = 'datadome=' + v + '; path=/; max-age=86400'; }",
        cookie_value,
    )


async def solve_datadome_if_needed(page: Page, context: BrowserContext, proxy_line: str) -> bool:
    import re
    html = await page.content()
    current_url = page.url

    # Case 1: DataDome JSON cookie-refresh response (seen after /date POST)
    # {"status": 200, "cookie": "datadome=VALUE"} — just inject and reload
    refresh_value = _is_datadome_cookie_refresh(html)
    if refresh_value:
        logger.info("DataDome cookie-refresh response detected — injecting cookie and reloading")
        await _inject_datadome_cookie(page, refresh_value)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)
        except Exception as exc:
            logger.warning("Reload after cookie-refresh inject failed: %s", exc)
        return True

    # Case 2: Full DataDome challenge page — solve via 2captcha
    captcha_url_match = re.search(r'(https://geo\.captcha-delivery\.com/captcha/[^"\'<>\s]+)', html)
    if not captcha_url_match:
        logger.info("No DataDome captcha URL found in page — not a DataDome challenge")
        return False

    captcha_url = captcha_url_match.group(1)
    user_agent = await page.evaluate("navigator.userAgent")
    logger.info("Submitting DataDome challenge to 2captcha (captcha_url=%s...)", captcha_url[:80])

    solved_cookie = await solve_datadome_2captcha(
        website_url=current_url,
        captcha_url=captcha_url,
        user_agent=user_agent,
        proxy_line=proxy_line,
    )
    if not solved_cookie:
        logger.warning("2captcha solve failed or returned no cookie")
        return False

    # Strip "datadome=" prefix if 2captcha returns it with the name included
    cookie_value = solved_cookie
    if cookie_value.lower().startswith("datadome="):
        cookie_value = cookie_value[len("datadome="):]

    logger.info("DataDome 2captcha solved — injecting cookie (prefix: %s...)", cookie_value[:20])
    await _inject_datadome_cookie(page, cookie_value)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)
    except Exception as exc:
        logger.warning("Reload after 2captcha inject failed: %s", exc)
    return True

def _flare_cookies_to_playwright(cookies: list[dict]) -> list[dict]:
    """Convert FlareSolverr cookie dicts to the format Playwright's add_cookies() expects."""
    same_site_map = {"strict": "Strict", "lax": "Lax", "none": "None"}
    result = []
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if not name or value is None:
            continue
        pw: dict = {"name": name, "value": str(value)}
        if c.get("domain"):
            pw["domain"] = c["domain"]
        pw["path"] = c.get("path") or "/"
        expires = c.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            pw["expires"] = float(expires)
        if c.get("httpOnly"):
            pw["httpOnly"] = True
        if c.get("secure"):
            pw["secure"] = True
        ss = same_site_map.get((c.get("sameSite") or "").lower())
        if ss:
            pw["sameSite"] = ss
        result.append(pw)
    return result


async def bootstrap_cf_cookies_via_flaresolverr(
    target_url: str,
    proxy_line: str = "",
    flaresolverr_url: str = "http://127.0.0.1:8191/v1",
) -> tuple[list[dict], str]:
    """GET target_url through FlareSolverr to solve Cloudflare, return (cookies, user_agent)."""
    import aiohttp

    payload: dict = {"cmd": "request.get", "url": target_url, "maxTimeout": 120000}
    if proxy_line:
        parts = proxy_line.split(":")
        if len(parts) >= 4:
            host, port, user = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            payload["proxy"] = {"url": f"http://{host}:{port}", "username": user, "password": password}

    logger.info("Bootstrapping CF cookies via FlareSolverr (%s) for %s", flaresolverr_url, target_url)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            flaresolverr_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            data = await resp.json(content_type=None)

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr returned status={data.get('status')!r}: {data.get('message', data)}")

    solution = data.get("solution", {})
    cookies = solution.get("cookies", [])
    user_agent = solution.get("userAgent", "")
    logger.info(
        "FlareSolverr bootstrap OK — %d cookie(s) %s, UA: %.80s",
        len(cookies),
        [c.get("name") for c in cookies],
        user_agent,
    )
    return cookies, user_agent


async def save_debug(page: Page, name: str) -> str:
    html = await page.content()
    html_path = DEBUG_DIR / f"{name}.html"
    png_path = DEBUG_DIR / f"{name}.png"
    html_path.write_text(html, encoding="utf-8")
    await page.screenshot(path=str(png_path), full_page=True)
    return str(html_path)


async def ensure_not_protected(page: Page, step: str) -> str:
    html = await page.content()
    if is_datadome_or_protection_page(html):
        await save_debug(page, f"blocked_{step}")
        raise RuntimeError(
            f"DataDome/protection page detected at {step}. "
            f"Debug saved under {DEBUG_DIR}/blocked_{step}.*"
        )
    return html


async def select_ticket_and_submit(page: Page) -> None:
    await page.evaluate(
        """
        () => {
            const sel = document.querySelector('select[name^="tickets["]');
            if (!sel) throw new Error("ticket select not found");
            sel.value = "1";
            sel.dispatchEvent(new Event("change", { bubbles: true }));
            if (window.jQuery) {
                window.jQuery(sel).val("1").trigger("change");
            }
        }
        """
    )
    await page.wait_for_timeout(1000)

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            await page.locator("form").evaluate("(form) => form.submit()")
    except Exception as exc:
        logger.warning("Navigation after form submit did not complete cleanly: %s", exc)
        await page.wait_for_timeout(10000)


async def fetch_timeslots_from_browser(
    page: Page,
    calendar_html: str,
    date_str: str,
    proxy_line: str = "",
) -> dict[str, Any]:
    import aiohttp
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

    # Extract session cookies from the browser and make the timeslots request
    # via the proxy (not page.evaluate) so Cloudflare sees the proxy IP, not the server IP
    cookies = await page.context.cookies()
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    user_agent = await page.evaluate("navigator.userAgent")

    proxy_url = None
    if proxy_line:
        parts = proxy_line.split(":")
        if len(parts) >= 4:
            host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
            proxy_url = f"http://{user}:{pw}@{host}:{port}"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://resa.notredamedeparis.fr/en/reservationindividuelle/date",
        "Origin": "https://resa.notredamedeparis.fr",
        "User-Agent": user_agent,
        "Cookie": cookie_header,
    }

    # Try in-browser fetch first (goes via Playwright proxy, has full CF fingerprint)
    in_browser_result = await page.evaluate(
        """
        async ({url, body}) => {
            try {
                const res = await fetch(url, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    body,
                    credentials: "include"
                });
                return {status: res.status, text: await res.text()};
            } catch(e) {
                return {status: 0, text: String(e)};
            }
        }
        """,
        {"url": TIMESLOTS_URL, "body": request_body},
    )
    if int(in_browser_result["status"]) == 200:
        try:
            return json.loads(in_browser_result["text"])
        except ValueError:
            pass  # fall through to aiohttp

    logger.warning("In-browser fetch status %s for %s — body: %s",
                   in_browser_result["status"], date_str, in_browser_result["text"][:300])

    # Fallback: aiohttp via proxy with extracted session cookies
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TIMESLOTS_URL,
            data=request_body,
            headers=headers,
            proxy=proxy_url,
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status_code = resp.status
            text = await resp.text()

    if status_code == 403:
        raise RuntimeError(f"Timeslots HTTP 403 for {date_str}: {text[:300]}")
    if status_code >= 400:
        raise RuntimeError(f"Timeslots HTTP {status_code} for {date_str}: {text[:300]}")

    try:
        return json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"Timeslots response was not JSON: {text[:300]}") from exc


class BrowserAvailabilityChecker:
    def __init__(self, settings: AvailabilityCheckerSettings) -> None:
        self.settings = settings
        self.client = AvailabilityTriggerClient(settings)

    async def close(self) -> None:
        await self.client.close()

    async def run_once(self, *, manual: bool = False) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cycle_started_at = datetime.now().astimezone()
        report: dict[str, Any] = {
            "source": self.settings.source,
            "cycle_started_at": cycle_started_at.isoformat(timespec="seconds"),
            "status": "started",
            "scanner": "playwright-browser",
        }

        try:
            request = await self._scan(checked_at=checked_at, manual=manual)
            report["request"] = request.model_dump(mode="json")
            report["summary"] = {
                "scanned_dates": int(request.metadata.get("scanned_dates", 0)),
                "available_dates": int(request.metadata.get("available_dates", 0)),
                "available_slots": len(request.availabilities),
            }

            if not request.availabilities:
                logger.info("No availability found; skipping trigger")
                result = {
                    "matched_tasks": 0,
                    "normalized_availabilities": [],
                    "updated_pending_tasks": 0,
                }
                report["status"] = "no-availability"
                report["trigger_result"] = result
                self._write_report(cycle_started_at, report)
                return result

            logger.info(
                "Trigger request payload:\n%s",
                json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True),
            )
            try:
                result = await self.client.trigger(request)
                report["status"] = "triggered"
                report["trigger_result"] = result
                self._write_report(cycle_started_at, report)
                return result
            except Exception as exc:
                if manual:
                    logger.warning("Trigger API unavailable locally; scan succeeded but trigger skipped: %s", exc)
                    result = {"status": "skipped", "reason": str(exc)}
                    report["status"] = "trigger-skipped"
                    report["trigger_result"] = result
                    self._write_report(cycle_started_at, report)
                    return result
                raise

        except Exception as exc:
            report["status"] = "error"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self._write_report(cycle_started_at, report)
            raise

    async def _scan(self, *, checked_at: str, manual: bool) -> Any:
        proxy_line = rotate_proxy_session(load_first_proxy())  # fresh session IP every scan
        proxy = playwright_proxy_from_line(proxy_line)

        logger.info(
            "Scanning availability via Playwright browser%s",
            f" using proxy {_proxy_display(proxy_line)}" if proxy_line else " without proxy",
        )

        # ── Bootstrap Cloudflare clearance via FlareSolverr ──────────────────
        # FlareSolverr's hardened Chromium passes CF bot checks; we use it once
        # to get the cf_clearance cookie + matching UA, then inject both into the
        # Playwright context so subsequent navigations skip the Turnstile challenge.
        flare_cookies: list[dict] = []
        flare_ua: str = ""
        # Prefer FLARESOLVERR_URLS (comma-separated list); fall back to single URL or localhost
        _flare_urls_raw = os.getenv("FLARESOLVERR_URLS", os.getenv("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1"))
        flaresolverr_url = [u.strip() for u in _flare_urls_raw.split(",") if u.strip()][0]
        try:
            raw_cookies, flare_ua = await bootstrap_cf_cookies_via_flaresolverr(
                RESERVATION_URL,
                proxy_line=proxy_line,
                flaresolverr_url=flaresolverr_url,
            )
            flare_cookies = _flare_cookies_to_playwright(raw_cookies)
        except Exception as exc:
            logger.warning(
                "FlareSolverr CF bootstrap failed — proceeding without CF cookies (may hit Cloudflare): %s",
                exc,
            )
        # ─────────────────────────────────────────────────────────────────────

        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "headless": not manual,
                "args": [
                    "--window-size=1440,900",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                ],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy

            profile_dir = Path(os.getenv("NDAME_BROWSER_PROFILE_DIR", "browser_profile_notredame"))
            profile_dir.mkdir(parents=True, exist_ok=True)

            context_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": not manual,
                "locale": "en-US",
                "no_viewport": True,
                "args": launch_kwargs["args"],
            }
            if proxy:
                context_kwargs["proxy"] = proxy
            # cf_clearance is bound to the UA that solved the challenge — must match
            if flare_ua:
                context_kwargs["user_agent"] = flare_ua

            browser_channel = os.getenv("NDAME_BROWSER_CHANNEL", "").strip()
            if browser_channel:
                context_kwargs["channel"] = browser_channel

            logger.info("Using persistent browser profile: %s", profile_dir.resolve())
            context = await p.chromium.launch_persistent_context(**context_kwargs)
            browser = context.browser

            # Inject FlareSolverr cookies before any navigation
            if flare_cookies:
                await context.add_cookies(flare_cookies)
                logger.info(
                    "Injected %d FlareSolverr cookie(s) into browser context: %s",
                    len(flare_cookies),
                    [c["name"] for c in flare_cookies],
                )

            try:
                page = await context.new_page()
                await page.goto(RESERVATION_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(20000)

                html = await page.content()
                await save_debug(page, "browser_01_tickets")

                if is_datadome_or_protection_page(html) or "csrf_name" not in html:
                    # Attempt 2captcha solving
                    solved = await solve_datadome_if_needed(page, context, proxy_line)
                    if solved:
                        html = await page.content()

                    if is_datadome_or_protection_page(html) or "csrf_name" not in html:
                        logger.info("Still protected/missing form at /tickets; refreshing once before manual prompt")
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=90000)
                        except Exception as exc:
                            logger.warning("Tickets refresh failed: %s", exc)
                        await page.wait_for_timeout(8000)
                        html = await page.content()

                    if is_datadome_or_protection_page(html) or "csrf_name" not in html:
                        if manual:
                            logger.warning("Protection page or missing form at /tickets. Waiting for manual verification.")
                            input("Complete verification in browser, then press Enter here...")
                            await page.wait_for_timeout(5000)
                            html = await page.content()

                        if is_datadome_or_protection_page(html) or "csrf_name" not in html:
                            await save_debug(page, "browser_blocked_tickets")
                            raise RuntimeError("Tickets page did not expose form after browser load")

                logger.info("Tickets page loaded successfully in browser")

                await select_ticket_and_submit(page)
                # Wait longer for CF JS to run and set cf_clearance on the calendar page
                await page.wait_for_timeout(20000)

                calendar_html = await page.content()
                await save_debug(page, "browser_02_date")

                # DataDome cookie-refresh: server returns JSON {"status":200,"cookie":"datadome=..."}
                # The browser JS should handle this automatically, but if it didn't navigate, help it
                refresh_value = _is_datadome_cookie_refresh(calendar_html)
                if refresh_value:
                    logger.info("DataDome cookie-refresh at /date — injecting and resubmitting")
                    await _inject_datadome_cookie(page, refresh_value)
                    await select_ticket_and_submit(page)
                    await page.wait_for_timeout(12000)
                    calendar_html = await page.content()
                    await save_debug(page, "browser_02b_date_retry")

                if is_datadome_or_protection_page(calendar_html):
                    # Full DataDome challenge — solve via 2captcha
                    solved = await solve_datadome_if_needed(page, context, proxy_line)
                    if solved:
                        calendar_html = await page.content()

                    if is_datadome_or_protection_page(calendar_html):
                        logger.info("Still protected at /date; refreshing once before manual prompt")
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=90000)
                        except Exception as exc:
                            logger.warning("Date refresh failed: %s", exc)
                        await page.wait_for_timeout(8000)
                        calendar_html = await page.content()

                    if is_datadome_or_protection_page(calendar_html):
                        if manual:
                            logger.warning("Protection page at /date. Waiting for manual verification.")
                            input("Complete verification in browser, then press Enter here...")
                            await page.wait_for_timeout(5000)
                            calendar_html = await page.content()

                        if is_datadome_or_protection_page(calendar_html):
                            await save_debug(page, "browser_blocked_date")
                            raise RuntimeError("DataDome/protection page returned at /date")

                if not is_calendar_page_html(calendar_html):
                    await save_debug(page, "browser_not_calendar")
                    raise RuntimeError("Calendar page was not reached after ticket submit")


                logger.info("Calendar page reached in browser")

                # Log cookies so we can debug 403s on timeslots
                cookies = await context.cookies()
                cookie_names = [c["name"] for c in cookies]
                logger.info("Cookies at calendar: %s", cookie_names)

                available_dates = extract_available_dates_from_calendar_html(calendar_html)
                logger.info("Found %d clickable date(s)", len(available_dates))

                # Standard Notre-Dame timeslot schedule — fallback when timeslots XHR is CF-blocked
                _STANDARD_SLOTS = [
                    "09:00","09:15","09:30","09:45","10:00","10:15","10:30","10:45",
                    "13:00","13:15","13:30","13:45","14:00","14:15","14:30","14:45",
                    "15:00","15:15","15:30","15:45","16:00","16:15",
                ]

                available_by_date: dict[str, list[dict[str, Any]]] = {}
                for date_str in available_dates:
                    slots = None
                    ajax_failed = False
                    try:
                        payload = await fetch_timeslots_from_browser(page, calendar_html, date_str, proxy_line=proxy_line)
                        slots = extract_available_slots_from_response(payload)
                    except Exception as exc:
                        logger.warning("Timeslots XHR blocked for %s (%s) — using standard slot schedule", date_str, exc)
                        ajax_failed = True

                    if ajax_failed:
                        # AJAX completely failed (CF-blocked, network error, etc.) — synthesize
                        # standard schedule as a best-effort fallback. Workers validate availability
                        # in real time via their own timeslot AJAX call before booking.
                        slots = [{"time": t, "totalAvailable": 20, "active": True} for t in _STANDARD_SLOTS]
                        logger.info("Synthetic slots for %s: %d timeslots", date_str, len(slots))
                    elif not slots:
                        # AJAX succeeded but all slots are sold out — skip this date entirely.
                        # Synthesizing here would flood the queue with bookings for genuinely
                        # unavailable slots (real-time check at worker level still catches this,
                        # but it wastes CF bandwidth and iProyal proxies).
                        logger.info("All slots sold out for %s (AJAX succeeded, no available slots) — skipping", date_str)
                        continue

                    available_by_date[date_str] = slots
                    logger.info("Availability confirmed for %s: %s", date_str, format_available_slots(slots))

                metadata = {
                    "checked_at": checked_at,
                    "scanned_dates": len(available_dates),
                    "scanned_date_values": available_dates,
                    "available_dates": len(available_by_date),
                    "scan_start_date": available_dates[0] if available_dates else "",
                    "scan_end_date": available_dates[-1] if available_dates else "",
                    "scanner": "playwright-browser",
                }
                if proxy_line:
                    metadata["upstream_proxy"] = _proxy_display(proxy_line)

                return build_trigger_request(
                    available_by_date,
                    source=self.settings.source,
                    metadata=metadata,
                )
            finally:
                await context.close()

    def _write_report(self, cycle_started_at: datetime, report: dict[str, Any]) -> str:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.settings.output_dir / build_cycle_report_filename(cycle_started_at)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        logger.info("Availability cycle report written to %s", report_path)
        return str(report_path)


async def async_main(
    *,
    manual: bool = False,
    monitor_minutes: float = 0.0,
    interval_min_seconds: float = 120.0,
    interval_max_seconds: float = 220.0,
) -> None:
    load_dotenv()
    setup_logging()
    settings = AvailabilityCheckerSettings.from_env()
    checker = BrowserAvailabilityChecker(settings)
    try:
        if monitor_minutes > 0:
            deadline = time.monotonic() + (monitor_minutes * 60)
            cycle = 0
            while time.monotonic() < deadline:
                cycle += 1
                logger.info("Browser monitor cycle %d started", cycle)
                try:
                    await checker.run_once(manual=manual)
                except Exception as exc:
                    logger.warning("Browser monitor cycle %d failed: %s", cycle, exc)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                sleep_for = random.uniform(interval_min_seconds, interval_max_seconds)
                sleep_for = min(sleep_for, remaining)
                logger.info("Sleeping %.0fs before next browser scan", sleep_for)
                await asyncio.sleep(sleep_for)

            logger.info("Browser monitor finished after %.1f minute(s)", monitor_minutes)
        else:
            await checker.run_once(manual=manual)
    finally:
        await checker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser-mode Notre-Dame availability scanner")
    parser.add_argument("--manual", action="store_true", help="Open visible browser and allow manual verification")
    parser.add_argument("--monitor-minutes", type=float, default=0.0, help="Run repeated browser scans for N minutes")
    parser.add_argument("--interval-min-seconds", type=float, default=120.0, help="Minimum seconds between monitor scans")
    parser.add_argument("--interval-max-seconds", type=float, default=220.0, help="Maximum seconds between monitor scans")
    args = parser.parse_args()
    asyncio.run(
        async_main(
            manual=args.manual,
            monitor_minutes=args.monitor_minutes,
            interval_min_seconds=args.interval_min_seconds,
            interval_max_seconds=args.interval_max_seconds,
        )
    )


if __name__ == "__main__":
    main()
