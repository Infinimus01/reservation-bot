"""
NDame Bot Scheduler
Reads start/stop times from the "Schedule" tab in the Google Sheet and
manages the ndame-availability systemd service accordingly.

Sheet tab name: Schedule
Headers (row 1): start_time | stop_time | date | enabled
Example row:     11:45      | 12:30     | 2026-06-29 | true
date can also be "daily" to repeat every day.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | scheduler | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ndame.scheduler")

SERVICE_NAME = "ndame-availability"
POLL_INTERVAL = 30
PARIS_TZ = ZoneInfo("Europe/Paris")
SCHEDULE_TAB = "Schedule"
PREWARM_MINUTES_BEFORE = 5
PREWARM_SESSIONS_FILE = Path("/opt/selenium_bot/prewarm_sessions.json")
PREWARM_TARGET_URL = "https://www.notredame-de-paris.com/visites"

SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS_FILE",
    "/opt/selenium_bot/service.json",
)
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")


def _authorized_session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    if CREDENTIALS_FILE and os.path.exists(CREDENTIALS_FILE):
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    elif CREDENTIALS_JSON:
        creds = Credentials.from_service_account_info(json.loads(CREDENTIALS_JSON), scopes=scopes)
    else:
        raise RuntimeError("No Google Sheets credentials found")
    return AuthorizedSession(creds)


def _read_schedule() -> list[dict[str, str]]:
    if not SPREADSHEET_ID:
        logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return []
    try:
        session = _authorized_session()
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
            f"/values/{SCHEDULE_TAB}!A1:E50"
        )
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("values", [])
        if len(rows) < 2:
            return []
        headers = [h.strip().lower() for h in rows[0]]
        result = []
        for row in rows[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            result.append(dict(zip(headers, padded)))
        return result
    except Exception as exc:
        logger.warning("Failed to read Schedule tab: %s", exc)
        return []


def _service_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def _start():
    logger.info("START: running systemctl start %s", SERVICE_NAME)
    subprocess.run(["systemctl", "start", SERVICE_NAME], check=False)


def _stop():
    logger.info("STOP: running systemctl stop %s", SERVICE_NAME)
    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)


def _flaresolverr_urls() -> list[str]:
    raw = os.getenv("FLARESOLVERR_URLS", "")
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def _prewarm_one(flare_url: str) -> str:
    """Create a FlareSolverr session and load the target URL. Returns session_id or ''."""
    base = flare_url.rstrip("/")
    v1 = base if base.endswith("/v1") else f"{base}/v1"
    r = _requests.post(v1, json={"cmd": "sessions.create"}, timeout=15)
    r.raise_for_status()
    sid = r.json().get("session", "")
    if not sid:
        return ""
    _requests.post(
        v1,
        json={"cmd": "request.get", "url": PREWARM_TARGET_URL, "session": sid, "maxTimeout": 60000},
        timeout=90,
    )
    return sid


def _prewarm_all(flare_urls: list[str]) -> None:
    if not _REQUESTS_OK:
        logger.warning("requests library not available — skipping FlareSolverr pre-warm")
        return
    sessions: dict[str, str] = {}
    for url in flare_urls:
        try:
            sid = _prewarm_one(url)
            if sid:
                sessions[url] = sid
                logger.info("Pre-warmed FlareSolverr session %s at %s", sid, url)
            else:
                logger.warning("Pre-warm session create returned no ID for %s", url)
        except Exception as exc:
            logger.warning("Pre-warm failed for %s: %s", url, exc)
    if sessions:
        try:
            PREWARM_SESSIONS_FILE.write_text(json.dumps(sessions))
            logger.info("Wrote %d pre-warm session(s) to %s", len(sessions), PREWARM_SESSIONS_FILE)
        except Exception as exc:
            logger.warning("Could not write prewarm sessions file: %s", exc)


def _parse_hm(t: str) -> tuple[int, int] | None:
    parts = t.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _date_matches(date_val: str, now: datetime) -> bool:
    v = date_val.strip().lower()
    if v in ("daily", "everyday", "*", ""):
        return True
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() == now.date()
    except ValueError:
        return False


_prewarm_done: set[str] = set()


def _prewarm_key(start: tuple[int, int], date_val: str) -> str:
    return f"{date_val}@{start[0]:02d}:{start[1]:02d}"


def _hm_sub5(h: int, m: int) -> tuple[int, int]:
    total = h * 60 + m - PREWARM_MINUTES_BEFORE
    return (total // 60) % 24, total % 60


def _tick():
    now = datetime.now(tz=PARIS_TZ)
    rows = _read_schedule()

    active_row = None
    for row in rows:
        if row.get("enabled", "true").strip().lower() in ("false", "0", "no", "off"):
            continue
        if _date_matches(row.get("date", "daily"), now):
            active_row = row
            break

    if not active_row:
        logger.debug("No active schedule entry for today (%s)", now.strftime("%Y-%m-%d"))
        return

    start = _parse_hm(active_row.get("start_time", ""))
    stop = _parse_hm(active_row.get("stop_time", ""))

    if not start or not stop:
        logger.warning("Invalid schedule row (bad start/stop time): %s", active_row)
        return

    current_hm = (now.hour, now.minute)
    # Overnight window support: if stop < start the window crosses midnight
    if stop < start:
        in_window = current_hm >= start or current_hm < stop
    else:
        in_window = start <= current_hm < stop
    running = _service_active()

    logger.debug(
        "Paris %02d:%02d | window %02d:%02d–%02d:%02d | bot=%s | in_window=%s",
        now.hour, now.minute,
        start[0], start[1], stop[0], stop[1],
        "running" if running else "stopped",
        in_window,
    )

    # Pre-warm FlareSolverr sessions PREWARM_MINUTES_BEFORE before start
    prewarm_hm = _hm_sub5(*start)
    pkey = _prewarm_key(start, active_row.get("date", "daily"))
    if prewarm_hm <= current_hm < start and pkey not in _prewarm_done and not running:
        _prewarm_done.add(pkey)
        flare_urls = _flaresolverr_urls()
        if flare_urls:
            logger.info(
                "Pre-warming %d FlareSolverr instance(s) — %d min before start at %02d:%02d",
                len(flare_urls), PREWARM_MINUTES_BEFORE, start[0], start[1],
            )
            _prewarm_all(flare_urls)
        else:
            logger.debug("No FLARESOLVERR_URLS set — skipping pre-warm")

    if in_window and not running:
        logger.info(
            "Schedule window %02d:%02d–%02d:%02d active, bot is stopped → starting",
            start[0], start[1], stop[0], stop[1],
        )
        _start()
    elif not in_window and running:
        logger.info(
            "Schedule window ended at %02d:%02d, bot is running → stopping",
            stop[0], stop[1],
        )
        _stop()
        # Clean up pre-warmed sessions file after bot stops
        if PREWARM_SESSIONS_FILE.exists():
            try:
                PREWARM_SESSIONS_FILE.unlink()
            except Exception:
                pass


def main():
    logger.info("NDame scheduler started (poll every %ds, Paris timezone)", POLL_INTERVAL)
    while True:
        try:
            _tick()
        except Exception as exc:
            logger.error("Unexpected scheduler error: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
