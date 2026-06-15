from __future__ import annotations

import asyncio
import base64

from flare_bot import FlareSession, _finish_stage, build_artifact_name
from util import UserDetails


def build_user_details(**overrides: object) -> UserDetails:
    defaults: dict[str, object] = {
        "unique_id": "task-001",
        "date": "2026-03-26",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "email": "ada@example.com",
        "phone": "1234567890",
        "zip": "97220",
        "country": "United States Of America",
        "time": "13:00",
        "ticket_count": 2,
        "job_time": "00:00",
        "status": "pending",
        "proxy": "",
        "upstream_proxy": "",
    }
    defaults.update(overrides)
    return UserDetails(**defaults)


def test_build_artifact_name_uses_step_worker_date_and_try_number() -> None:
    name = build_artifact_name(
        "confirmation page",
        build_user_details(),
        {"worker_name": "worker alpha", "try_number": 3},
    )

    assert name == "confirmation-page_worker-alpha_2026-03-26_try3"


def test_build_artifact_name_defaults_try_number_to_one() -> None:
    name = build_artifact_name(
        "confirmation",
        build_user_details(date="bad-date"),
        {"worker_id": "worker-7", "retry_count": 0},
    )

    assert name.startswith("confirmation_worker-7_")
    assert name.endswith("_try1")


def test_save_screenshot_writes_png_and_html(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("flare_bot.SCREENSHOTS_DIR", tmp_path)
    session = FlareSession(flaresolverr_url="http://127.0.0.1:8191/v1", instance_id=4)
    session.session_id = "session-123"
    session.last_url = "https://example.invalid/confirmation"
    session.last_html = "<html>before</html>"

    async def fake_get(
        url: str,
        timeout: int | None = None,
        return_screenshot: bool = False,
    ) -> object:
        assert url == session.last_url
        assert return_screenshot is True
        session.last_html = "<html>after</html>"
        session.last_screenshot_base64 = base64.b64encode(b"png-bytes").decode()
        return object()

    monkeypatch.setattr(session, "get", fake_get)

    asyncio.run(session.save_screenshot("confirmation_worker-1_2026-03-26_try2"))

    saved_files = list(tmp_path.iterdir())
    assert len(saved_files) == 2
    assert any(path.suffix == ".png" for path in saved_files)
    assert any(path.suffix == ".html" for path in saved_files)
    png_path = next(path for path in saved_files if path.suffix == ".png")
    html_path = next(path for path in saved_files if path.suffix == ".html")
    assert png_path.read_bytes() == b"png-bytes"
    assert "after" in html_path.read_text(encoding="utf-8")


def test_finish_stage_uses_explicit_stage_without_shared_status_dict() -> None:
    payloads: list[dict[str, object]] = []

    _finish_stage(
        instance_id=4,
        outcome="success",
        status_dict=None,
        status_callback=payloads.append,
        stage="STEP 6/6: Confirmation",
    )

    assert payloads[0]["stage"] == "STEP 6/6: Confirmation"
