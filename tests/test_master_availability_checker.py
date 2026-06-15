from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flare_bot import build_ajax_post_headers
from master.availability_checker import (
    AvailabilityChecker,
    AvailabilityBackoffError,
    TimeslotsForbiddenError,
    build_trigger_request,
    build_cycle_report_filename,
    extract_available_dates_from_calendar_html,
    extract_available_slots_from_response,
    format_available_slots,
    is_calendar_page_html,
)
from shared.config import AvailabilityCheckerSettings
from shared.models import AvailabilityTriggerRequest


@pytest.fixture(autouse=True)
def clear_iproyal_proxy_env(monkeypatch) -> None:
    monkeypatch.delenv("IPROYAL_PROXY", raising=False)


def test_extract_available_dates_from_calendar_html_uses_clickable_cells() -> None:
    html = """
    <table>
        <tr>
            <td class="ui-datepicker-week-end ui-datepicker-unselectable ui-state-disabled disabled">
                <span class="ui-state-default">29</span>
            </td>
            <td class="ui-datepicker-days-cell-over ui-datepicker-current-day ui-datepicker-today"
                data-handler="selectDay"
                data-event="click"
                data-month="2"
                data-year="2026">
                <a class="ui-state-default ui-state-highlight ui-state-active" href="#">30</a>
            </td>
            <td data-handler="selectDay" data-event="click" data-month="3" data-year="2026">
                <a class="ui-state-default" href="#">1</a>
            </td>
        </tr>
    </table>
    """

    dates = extract_available_dates_from_calendar_html(html)

    assert dates == [
        "2026-03-30",
        "2026-04-01",
    ]


def test_extract_available_dates_from_calendar_html_falls_back_to_window_data() -> None:
    html = """
    <script>
        var ticketMinDate = new Date(2026, 2, 30);
        var ticketMaxDate = new Date(2026, 3, 1);
        var disabledWeekDays = [];
        var disabledDates = ["2026-03-31"];
        var soldoutDates = [];
    </script>
    """

    dates = extract_available_dates_from_calendar_html(html)

    assert dates == [
        "2026-03-30",
        "2026-04-01",
    ]


def test_extract_available_slots_from_response_uses_positive_quantities() -> None:
    slots = extract_available_slots_from_response(
        {
            "success": True,
            "date": "2026-03-30",
            "timeslots": {
                "09:00": {
                    "active": False,
                    "soldOut": True,
                    "time": "09:00",
                    "totalAvailable": 0,
                    "classAttr": "timeslotFull",
                },
                "09:15": {
                    "active": False,
                    "soldOut": True,
                    "time": "09:15",
                    "totalAvailable": -5,
                    "classAttr": "timeslotFull",
                },
                "10:00": {
                    "active": True,
                    "soldOut": False,
                    "time": "10:00",
                    "totalAvailable": 4,
                    "classAttr": "timeslotBusy",
                },
                "10:15": {
                    "active": True,
                    "soldOut": False,
                    "time": "10:15",
                    "totalAvailable": "2",
                    "classAttr": "timeslotQuiet",
                },
            },
        }
    )

    assert slots == [
        {
            "time": "10:00",
            "class": "timeslotBusy",
            "totalAvailable": 4,
            "active": True,
        },
        {
            "time": "10:15",
            "class": "timeslotQuiet",
            "totalAvailable": 2,
            "active": True,
        },
    ]


def test_is_calendar_page_html_rejects_invalid_csrf_error_wrapper() -> None:
    html = (
        '<html><body><pre>{"message":"Invalid CSRF token provided"}</pre></body></html>'
    )

    assert is_calendar_page_html(html) is False


def test_is_calendar_page_html_accepts_datepicker_markup() -> None:
    html = """
    <html>
        <body>
            <script>
                var ticketMinDate = new Date(2026, 2, 30);
                var ticketMaxDate = new Date(2026, 2, 31);
            </script>
            <table class="ui-datepicker-calendar">
                <tbody>
                    <tr>
                        <td data-handler="selectDay" data-event="click" data-month="2" data-year="2026">
                            <a href="#">30</a>
                        </td>
                    </tr>
                </tbody>
            </table>
        </body>
    </html>
    """

    assert is_calendar_page_html(html) is True


def test_build_trigger_request_formats_master_contract_payload() -> None:
    request = build_trigger_request(
        {
            "2026-03-26": [
                {"time": "16:30", "totalAvailable": "2"},
                {"time": "15:00", "totalAvailable": 4},
                {"time": "18:00", "totalAvailable": 0},
            ],
            "2026-03-27": [
                {"time": "09:00", "totalAvailable": "1.0"},
                {"time": "", "totalAvailable": 3},
            ],
        },
        source="master-availability-checker",
        metadata={"scanned_dates": 2},
    )

    assert request.model_dump() == {
        "availabilities": [
            {"date": "2026/03/26", "time": "15:00", "quantity": 4},
            {"date": "2026/03/26", "time": "16:30", "quantity": 2},
            {"date": "2026/03/27", "time": "09:00", "quantity": 1},
        ],
        "source": "master-availability-checker",
        "metadata": {"scanned_dates": 2},
    }


def test_availability_checker_settings_builds_master_url_from_master_host() -> None:
    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_HOST": "0.0.0.0",
            "MASTER_PORT": "9000",
            "MASTER_API_KEY": "secret",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": "./availability_runs",
        }
    )

    assert settings.master_url == "http://127.0.0.1:9000"
    assert settings.master_api_key == "secret"
    assert settings.poll_interval_seconds == 60.0
    assert settings.output_dir == Path("availability_runs").resolve()


def test_build_cycle_report_filename_uses_run_timestamp() -> None:
    filename = build_cycle_report_filename(
        datetime(2026, 3, 30, 13, 25, 10, tzinfo=timezone.utc)
    )

    assert filename == "availability_2026-03-30_13-25-10.json"


def test_build_ajax_post_headers_match_timeslots_contract() -> None:
    headers = build_ajax_post_headers(
        "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
    )

    assert headers == {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://resa.notredamedeparis.fr",
        "Referer": "https://resa.notredamedeparis.fr/en/reservationindividuelle/date",
        "X-Requested-With": "XMLHttpRequest",
    }


def test_format_available_slots_lists_times_quantities_and_classes() -> None:
    formatted = format_available_slots(
        [
            {"time": "20:15", "totalAvailable": 3, "class": "timeslotBusy"},
            {"time": "09:00", "totalAvailable": "1", "class": "timeslotQuiet"},
        ]
    )

    assert formatted == (
        "09:00 (qty=1, class=timeslotQuiet), "
        "20:15 (qty=3, class=timeslotBusy)"
    )


def test_availability_checker_run_once_skips_trigger_when_no_availability(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": str(tmp_path),
        }
    )
    checker = AvailabilityChecker(settings)

    request = AvailabilityTriggerRequest(
        source="master-availability-checker",
        metadata={"scanned_dates": 3, "available_dates": 0},
        availabilities=[],
    )

    async def fake_scan() -> AvailabilityTriggerRequest:
        return request

    async def fake_close() -> None:
        return None

    async def fake_trigger(_request: AvailabilityTriggerRequest) -> dict[str, object]:
        raise AssertionError("trigger should not be called when no availability exists")

    monkeypatch.setattr(checker, "_scan_availability", fake_scan)
    monkeypatch.setattr(
        checker,
        "_write_cycle_report",
        lambda *_args, **_kwargs: str(tmp_path / "availability.json"),
    )
    checker.client.trigger = fake_trigger  # type: ignore[method-assign]
    checker.client.close = fake_close  # type: ignore[method-assign]

    result = asyncio.run(checker.run_once())

    assert result["matched_tasks"] == 0
    assert result["updated_pending_tasks"] == 0
    assert result["normalized_availabilities"] == []
    assert result["report_file"] == str(tmp_path / "availability.json")


def test_availability_checker_run_once_triggers_master_when_availability_exists(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": str(tmp_path),
        }
    )
    checker = AvailabilityChecker(settings)

    request = AvailabilityTriggerRequest(
        source="master-availability-checker",
        metadata={"scanned_dates": 2, "available_dates": 1},
        availabilities=[
            {"date": "2026/03/30", "time": "10:00", "quantity": 2},
        ],
    )
    calls: list[AvailabilityTriggerRequest] = []

    async def fake_scan() -> AvailabilityTriggerRequest:
        return request

    async def fake_close() -> None:
        return None

    async def fake_trigger(payload: AvailabilityTriggerRequest) -> dict[str, object]:
        calls.append(payload)
        return {
            "matched_tasks": 2,
            "updated_pending_tasks": 2,
            "normalized_availabilities": [
                {"date": "2026/03/30", "time": "10:00", "quantity": 2},
            ],
        }

    monkeypatch.setattr(checker, "_scan_availability", fake_scan)
    monkeypatch.setattr(
        checker,
        "_write_cycle_report",
        lambda *_args, **_kwargs: str(tmp_path / "availability.json"),
    )
    checker.client.trigger = fake_trigger  # type: ignore[method-assign]
    checker.client.close = fake_close  # type: ignore[method-assign]

    with caplog.at_level("INFO", logger="flare_bot.availability_checker"):
        result = asyncio.run(checker.run_once())

    assert calls == [request]
    assert result["matched_tasks"] == 2
    assert result["updated_pending_tasks"] == 2
    assert result["report_file"] == str(tmp_path / "availability.json")
    assert "Trigger request payload:" in caplog.text
    assert '"availabilities"' in caplog.text
    assert '"time": "10:00"' in caplog.text


def test_fetch_timeslots_payload_raises_on_first_403_without_same_session_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": str(tmp_path),
        }
    )
    checker = AvailabilityChecker(settings)

    class FakeForbiddenResponse:
        status_code = 403
        text = "forbidden"

        def json(self) -> dict[str, object]:
            raise AssertionError("json() should not be called for HTTP 403")

    class FakeDirectSession:
        def __init__(self) -> None:
            self.calls = 0

        def post_form(self, *_args, **_kwargs) -> FakeForbiddenResponse:
            self.calls += 1
            return FakeForbiddenResponse()

    fake_direct_session = FakeDirectSession()

    with pytest.raises(TimeslotsForbiddenError):
        asyncio.run(
            checker._fetch_timeslots_payload(
                fake_direct_session,
                "<html></html>",
                "2026-04-09",
            )
        )

    assert fake_direct_session.calls == 1


def test_availability_checker_generates_warmed_iproyal_proxy(
    monkeypatch,
) -> None:
    warmed: list[tuple[str, float]] = []
    tokens = iter(["AAAA1111", "BBBB2222"])

    async def fake_check_proxy(proxy: str, timeout: float = 15.0) -> bool:
        warmed.append((proxy, timeout))
        return True

    monkeypatch.setenv("IPROYAL_PROXY", "geo.iproyal.com:12321:user:pass")
    monkeypatch.setenv("IPROYAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("IPROYAL_PROXY_LIFETIME", "24h")
    monkeypatch.setenv("IPROYAL_PROXY_WARMUP_ATTEMPTS", "1")
    monkeypatch.setenv("IPROYAL_PROXY_WARMUP_TIMEOUT_SECONDS", "9")
    monkeypatch.setattr("shared.iproyal_proxy._session_token", lambda: next(tokens))
    monkeypatch.setattr("master.availability_checker.check_proxy", fake_check_proxy)
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: pytest.fail("IPRoyal mode should not load proxies.txt"),
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": ".",
        }
    )
    checker = AvailabilityChecker(settings)

    first_proxy = asyncio.run(checker._get_upstream_proxy())
    second_proxy = asyncio.run(checker._get_upstream_proxy())

    assert first_proxy == (
        "geo.iproyal.com:12321:user:pass_country-us_session-AAAA1111_lifetime-24h"
    )
    assert second_proxy == (
        "geo.iproyal.com:12321:user:pass_country-us_session-BBBB2222_lifetime-24h"
    )
    assert warmed == [(first_proxy, 9.0), (second_proxy, 9.0)]


def test_scan_availability_retries_403_with_new_proxy_and_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: ["proxy-a", "proxy-b"],
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1", "http://localhost:8192/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": str(tmp_path),
        }
    )
    checker = AvailabilityChecker(settings)
    proxy_calls: list[tuple[bool, set[str]]] = []
    scan_calls: list[tuple[str, str]] = []

    async def fake_get_upstream_proxy(
        *,
        force_new: bool = False,
        exclude: set[str] | None = None,
    ) -> str:
        proxy_calls.append((force_new, set(exclude or set())))
        return "proxy-b" if force_new else "proxy-a"

    request = AvailabilityTriggerRequest(
        source="master-availability-checker",
        metadata={"scanned_dates": 1, "available_dates": 1},
        availabilities=[
            {"date": "2026/04/09", "time": "10:00", "quantity": 1},
        ],
    )

    async def fake_scan_once(
        *,
        flare_url: str,
        upstream_proxy: str,
        checked_at: str,
    ) -> AvailabilityTriggerRequest:
        scan_calls.append((flare_url, upstream_proxy))
        if len(scan_calls) == 1:
            raise TimeslotsForbiddenError("2026-04-09")
        assert checked_at
        return request

    monkeypatch.setattr(checker, "_get_upstream_proxy", fake_get_upstream_proxy)
    monkeypatch.setattr(checker, "_scan_availability_once", fake_scan_once)

    result = asyncio.run(checker._scan_availability())

    assert result == request
    assert proxy_calls == [
        (False, set()),
        (True, {"proxy-a"}),
    ]
    assert scan_calls == [
        ("http://localhost:8191/v1", "proxy-a"),
        ("http://localhost:8192/v1", "proxy-b"),
    ]


def test_run_forever_waits_for_poll_interval_after_backoff_error(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    monkeypatch.setattr(
        "master.availability_checker.load_proxies_from_file",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "master.availability_checker.get_flaresolverr_urls",
        lambda *_args, **_kwargs: ["http://localhost:8191/v1"],
    )

    settings = AvailabilityCheckerSettings.from_env(
        {
            "MASTER_URL": "http://127.0.0.1:8000",
            "AVAILABILITY_CHECKER_OUTPUT_DIR": str(tmp_path),
            "AVAILABILITY_CHECKER_POLL_INTERVAL_SECONDS": "15",
        }
    )
    checker = AvailabilityChecker(settings)
    sleep_calls: list[float] = []

    async def fake_run_once() -> dict[str, object]:
        raise AvailabilityBackoffError("back off until next interval")

    class StopLoop(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        raise StopLoop()

    monkeypatch.setattr(checker, "run_once", fake_run_once)
    monkeypatch.setattr("master.availability_checker.asyncio.sleep", fake_sleep)

    with caplog.at_level("WARNING", logger="flare_bot.availability_checker"):
        with pytest.raises(StopLoop):
            asyncio.run(checker.run_forever())

    assert sleep_calls == [15.0]
    assert "back off until next interval" in caplog.text
