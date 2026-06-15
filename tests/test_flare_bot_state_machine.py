from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import flare_bot as flare_bot_module
from flare_bot import (
    CalendarPageSession,
    DEFAULT_PAYMENT_TURNSTILE_SITEKEY,
    DetailsPageSession,
    DonationPageSession,
    HomePageSession,
    PaymentPageSession,
)
from util import UserDetails


class FakeFlareSession:
    def __init__(self, html: str) -> None:
        self.last_html = html
        self.last_url = (
            "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets"
        )
        self.last_status = 200
        self.last_cookies: list[object] = []
        self.last_user_agent = "pytest-agent"
        self.last_screenshot_base64 = ""
        self.posts: list[dict[str, object]] = []
        self.browser_posts: list[dict[str, object]] = []
        self.browser_gets: list[dict[str, object]] = []

    async def get(
        self,
        url: str,
        timeout: int,
        return_screenshot: bool = False,
        headers: dict[str, str] | None = None,
        sync_cookies: bool = False,
    ) -> object:
        raise AssertionError("FlareSolverr GET should not be used in this test")

    async def post(
        self,
        url: str,
        post_data: str,
        timeout: int,
        headers: dict[str, str] | None = None,
        sync_cookies: bool = False,
    ) -> object:
        raise AssertionError("FlareSolverr POST should not be used for form submits")

    async def post_direct_form(
        self,
        url: str,
        post_data: str,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> object:
        self.posts.append(
            {
                "url": url,
                "post_data": post_data,
                "timeout": timeout,
                "headers": headers,
            }
        )
        self.last_url = "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
        self.last_html = "<html><body><form><input name='csrf_name' value='calendar'></form></body></html>"
        return SimpleNamespace(ok=True, message="")


def test_home_page_session_uses_saved_tickets_html_and_posts_to_calendar() -> None:
    tickets_html = Path("tests/fixtures/tickets_page.html").read_text(encoding="utf-8")
    session = FakeFlareSession(tickets_html)
    home_page = HomePageSession(session, instance_id=1)

    asyncio.run(home_page.wait_for_load())
    asyncio.run(home_page.select_tickets_and_submit(2))

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
    payload = parse_qs(str(post["post_data"]))
    assert payload["csrf_name"] == ["csrf-ticket-name"]
    assert payload["csrf_value"] == ["csrf-ticket-value"]
    assert payload["token_tickets"] == ["ticket-token-123"]
    assert payload["tickets[411622]"] == ["2"]
    assert payload["donation-input"] == ["0"]
    assert payload["donationCheck"] == ["true"]
    assert post["headers"] == {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://resa.notredamedeparis.fr",
        "Referer": "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets",
    }
    assert session.last_url.endswith("/date")


def test_calendar_page_can_blind_submit_without_fetching_timeslots(monkeypatch) -> None:
    calendar_html = """
    <html>
      <body>
        <form>
          <input type="hidden" name="csrf_name" value="calendar-name" />
          <input type="hidden" name="csrf_value" value="calendar-value" />
        </form>
      </body>
    </html>
    """

    class FakeCalendarSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(calendar_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
            )

        async def post_direct_form(
            self,
            url: str,
            post_data: str,
            timeout: int,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.posts.append(
                {
                    "url": url,
                    "post_data": post_data,
                    "timeout": timeout,
                    "headers": headers,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/personal-details"
            )
            self.last_html = """
            <html>
              <body>
                <input name="firstName" value="" />
                <input name="surname" value="" />
                <input name="emailAddress" value="" />
              </body>
            </html>
            """
            return SimpleNamespace(ok=True, message="")

    async def should_not_be_called(*_args, **_kwargs):
        raise AssertionError("get_available_timeslots should not be called in blind-submit mode")

    session = FakeCalendarSession()
    calendar_page = CalendarPageSession(session, instance_id=2)
    monkeypatch.setattr(calendar_page, "get_available_timeslots", should_not_be_called)

    asyncio.run(
        calendar_page.select_time_and_submit(
            "2026-03-26",
            "13:00",
            1,
            check_availability=False,
        )
    )

    assert len(session.posts) == 1
    post = session.posts[0]
    payload = parse_qs(str(post["post_data"]))
    assert payload["csrf_name"] == ["calendar-name"]
    assert payload["csrf_value"] == ["calendar-value"]
    assert payload["ticketDate"] == ["2026-03-26"]
    assert payload["ticketTime"] == ["13:00"]
    assert post["headers"] == {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://resa.notredamedeparis.fr",
        "Referer": "https://resa.notredamedeparis.fr/en/reservationindividuelle/date",
    }


def test_home_page_session_raises_explicit_error_for_invalid_csrf_response() -> None:
    tickets_html = Path("tests/fixtures/tickets_page.html").read_text(encoding="utf-8")

    class InvalidCsrfSession(FakeFlareSession):
        async def post_direct_form(
            self,
            url: str,
            post_data: str,
            timeout: int,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.posts.append(
                {
                    "url": url,
                    "post_data": post_data,
                    "timeout": timeout,
                    "headers": headers,
                }
            )
            self.last_url = "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
            self.last_html = (
                '<html><body><pre>{"message":"Invalid CSRF token provided"}</pre></body></html>'
            )
            return SimpleNamespace(ok=True, message="")

    session = InvalidCsrfSession(tickets_html)
    home_page = HomePageSession(session, instance_id=3)

    asyncio.run(home_page.wait_for_load())

    try:
        asyncio.run(home_page.select_tickets_and_submit(1))
    except Exception as exc:
        assert str(exc) == "Tickets submit rejected by site: invalid CSRF token"
    else:
        raise AssertionError("Expected explicit invalid CSRF exception")


def test_donation_page_submits_via_direct_http_session() -> None:
    donation_html = """
    <html>
      <body>
        <form>
          <input type="hidden" name="csrf_name" value="donation-name" />
          <input type="hidden" name="csrf_value" value="donation-value" />
        </form>
      </body>
    </html>
    """

    class DirectDonationSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(donation_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/donation"
            )

        async def post_direct_form(
            self,
            url: str,
            post_data: str,
            timeout: int,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.posts.append(
                {
                    "url": url,
                    "post_data": post_data,
                    "timeout": timeout,
                    "headers": headers,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )
            self.last_html = "<html><body>payment</body></html>"
            return SimpleNamespace(ok=True, message="")

    session = DirectDonationSession()
    donation_page = DonationPageSession(session, instance_id=4)

    asyncio.run(donation_page.skip_and_submit())

    assert len(session.posts) == 1
    assert session.browser_posts == []
    assert session.browser_gets == []
    post = session.posts[0]
    assert post["url"] == "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
    payload = parse_qs(str(post["post_data"]))
    assert payload["csrf_name"] == ["donation-name"]
    assert payload["csrf_value"] == ["donation-value"]
    assert payload["donation-input"] == ["0"]
    assert payload["donationCheck"] == ["true"]


def test_details_page_uses_hardcoded_uk_international_phone_number() -> None:
    details_html = """
    <html>
      <body>
        <form>
          <input type="hidden" name="csrf_name" value="details-name" />
          <input type="hidden" name="csrf_value" value="details-value" />
          <select name="country">
            <option value="">Choose a country</option>
            <option value="US">United States Of America</option>
            <option value="CA">Canada</option>
          </select>
        </form>
      </body>
    </html>
    """

    class DetailsSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(details_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/personal-details"
            )

        async def post_direct_form(
            self,
            url: str,
            post_data: str,
            timeout: int,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.posts.append(
                {
                    "url": url,
                    "post_data": post_data,
                    "timeout": timeout,
                    "headers": headers,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )
            self.last_html = "<html><body>payment</body></html>"
            return SimpleNamespace(ok=True, message="")

    session = DetailsSession()
    details_page = DetailsPageSession(session, instance_id=5)
    user_details = UserDetails(
        unique_id="test-user",
        date="2026-04-11",
        firstName="Grace",
        lastName="Clark",
        email="grace@example.com",
        phone="3038624090",
        zip="80202",
        country="United States Of America",
        time="10:00",
        ticket_count=1,
        job_time="",
        status="pending",
        proxy="",
        upstream_proxy="",
    )

    asyncio.run(details_page.fill_and_submit(user_details))

    assert len(session.posts) == 1
    assert session.posts[0]["url"] == "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
    payload = parse_qs(str(session.posts[0]["post_data"]))
    assert payload["country"] == ["US"]
    assert payload["phoneNumber"] == ["3038624090"]
    assert payload["phone-number"] == ["+443038624090"]


def test_payment_page_fails_fast_when_order_limit_is_reached() -> None:
    payment_html = """
    <html>
      <body>
        <div class="error-container orderLimitReached">
          <span>Maximum amount of orders has been reached.</span>
        </div>
        <form>
          <input type="hidden" name="csrf_name" value="payment-name" />
          <input type="hidden" name="csrf_value" value="payment-value" />
        </form>
      </body>
    </html>
    """

    class PaymentSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(payment_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )

        async def get(
            self,
            url: str,
            timeout: int,
            return_screenshot: bool = False,
            headers: dict[str, str] | None = None,
            sync_cookies: bool = False,
        ) -> object:
            self.browser_gets.append(
                {
                    "url": url,
                    "timeout": timeout,
                    "return_screenshot": return_screenshot,
                    "headers": headers,
                    "sync_cookies": sync_cookies,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )
            self.last_html = payment_html
            return SimpleNamespace(ok=True, message="")

    session = PaymentSession()
    payment_page = PaymentPageSession(session, instance_id=6)

    try:
        asyncio.run(payment_page.accept_terms_and_complete())
    except Exception as exc:
        assert (
            str(exc)
            == "Payment page blocked by site: slot capacity reached | Maximum amount of orders has been reached."
        )
    else:
        raise AssertionError("Expected payment page capacity exception")

    assert session.posts == []
    assert len(session.browser_gets) == 1


def test_payment_page_submits_via_flaresolverr_browser(monkeypatch) -> None:
    payment_html = """
    <html>
      <body>
        <form>
          <input type="hidden" name="csrf_name" value="payment-name" />
          <input type="hidden" name="csrf_value" value="payment-value" />
        </form>
      </body>
    </html>
    """

    class PaymentSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(payment_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )

        async def get(
            self,
            url: str,
            timeout: int,
            return_screenshot: bool = False,
            headers: dict[str, str] | None = None,
            sync_cookies: bool = False,
        ) -> object:
            self.browser_gets.append(
                {
                    "url": url,
                    "timeout": timeout,
                    "return_screenshot": return_screenshot,
                    "headers": headers,
                    "sync_cookies": sync_cookies,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )
            self.last_html = payment_html
            return SimpleNamespace(ok=True, message="")

        async def post_direct_form(
            self,
            url: str,
            post_data: str,
            timeout: int,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.posts.append(
                {
                    "url": url,
                    "post_data": post_data,
                    "timeout": timeout,
                    "headers": headers,
                }
            )
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/thank-you?orderHash=test"
            )
            self.last_html = "<html><body>thank you</body></html>"
            return SimpleNamespace(ok=True, message="")

    session = PaymentSession()
    payment_page = PaymentPageSession(session, instance_id=7)
    monkeypatch.setattr(payment_page, "_solve_turnstile", lambda: asyncio.sleep(0, result="token-123"))

    asyncio.run(payment_page.accept_terms_and_complete())

    assert len(session.browser_gets) == 1
    browser_get = session.browser_gets[0]
    assert browser_get["url"] == "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
    assert browser_get["sync_cookies"] is True
    assert len(session.posts) == 1
    assert session.browser_posts == []
    post = session.posts[0]
    assert post["url"] == "https://resa.notredamedeparis.fr/en/reservationindividuelle/thank-you"
    payload = parse_qs(str(post["post_data"]))
    assert payload["csrf_name"] == ["payment-name"]
    assert payload["csrf_value"] == ["payment-value"]
    assert payload["adyen-data"] == ["[]"]
    assert payload["terms-and-conditions"] == ["on"]
    assert payload["paymentCheck"] == ["true"]
    assert payload["cf-turnstile-response"] == ["token-123"]


def test_payment_page_turnstile_solver_falls_back_to_known_sitekey(monkeypatch) -> None:
    payment_html = """
    <html>
      <body>
        <form>
          <input type="hidden" name="csrf_name" value="payment-name" />
          <input type="hidden" name="csrf_value" value="payment-value" />
        </form>
      </body>
    </html>
    """

    class PaymentSession(FakeFlareSession):
        def __init__(self) -> None:
            super().__init__(payment_html)
            self.last_url = (
                "https://resa.notredamedeparis.fr/en/reservationindividuelle/payment"
            )

    class FakeAioHttpResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        async def __aenter__(self) -> "FakeAioHttpResponse":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def json(self, content_type=None) -> dict[str, object]:
            return self.payload

    class FakeAioHttpSession:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        async def __aenter__(self) -> "FakeAioHttpSession":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> FakeAioHttpResponse:
            self.requests.append({"url": url, "json": json})
            if url.endswith("/createTask"):
                return FakeAioHttpResponse({"errorId": 0, "taskId": "task-1"})
            return FakeAioHttpResponse(
                {
                    "status": "ready",
                    "solution": {"token": "fallback-token-123"},
                }
            )

    async def fake_sleep(_seconds: float) -> None:
        return None

    fake_http = FakeAioHttpSession()
    session = PaymentSession()
    payment_page = PaymentPageSession(session, instance_id=8)

    monkeypatch.setenv("CAPSOLVER_API_KEY", "capsolver-key")
    monkeypatch.setattr(flare_bot_module.aiohttp, "ClientSession", lambda *args, **kwargs: fake_http)
    monkeypatch.setattr(flare_bot_module.asyncio, "sleep", fake_sleep)

    token = asyncio.run(payment_page._solve_turnstile())

    assert token == "fallback-token-123"
    assert fake_http.requests[0]["json"]["task"]["websiteKey"] == DEFAULT_PAYMENT_TURNSTILE_SITEKEY
