from __future__ import annotations

import asyncio
import logging
import re

from shared.iproyal_proxy import (
    IPRoyalProxySettings,
    acquire_warmed_iproyal_proxy,
    build_iproyal_sticky_proxy,
    proxy_display,
)


def test_build_iproyal_sticky_proxy_replaces_existing_session_tags() -> None:
    proxy = build_iproyal_sticky_proxy(
        "geo.iproyal.com:12321:user:pass_country-gb_session-ABCDEFGH_lifetime-1h",
        country="us",
        lifetime="24h",
    )

    assert proxy.startswith("geo.iproyal.com:12321:user:pass_country-us_session-")
    assert proxy.endswith("_lifetime-24h")
    assert "_country-gb" not in proxy
    assert "_session-ABCDEFGH" not in proxy
    assert re.search(r"_session-[A-Za-z0-9]{8}_lifetime-24h$", proxy)


def test_proxy_display_masks_password_and_shows_sticky_metadata() -> None:
    display = proxy_display(
        "geo.iproyal.com:12321:user:secret_country-us_session-ABCDEFGH_lifetime-24h"
    )

    assert display == (
        "geo.iproyal.com:12321:user:*** "
        "(session=ABCDEFGH, country=us, lifetime=24h)"
    )
    assert "secret" not in display


def test_iproyal_settings_from_env_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("IPROYAL_PROXY", raising=False)

    settings = IPRoyalProxySettings.from_env()

    assert not settings.enabled


def test_acquire_warmed_iproyal_proxy_retries_until_warm(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    async def fake_warmup(proxy: str, timeout: float) -> bool:
        calls.append((proxy, timeout))
        return len(calls) == 2

    settings = IPRoyalProxySettings(
        base_proxy="geo.iproyal.com:12321:user:pass",
        country="us",
        lifetime="24h",
        warmup_enabled=True,
        warmup_attempts=2,
        warmup_timeout_seconds=7,
    )

    proxy = asyncio.run(
        acquire_warmed_iproyal_proxy(
            settings,
            warmup=fake_warmup,
            logger=logging.getLogger("test"),
            context="test",
        )
    )

    assert proxy == calls[-1][0]
    assert calls[0][0] != calls[1][0]
    assert calls[0][1] == 7
    assert calls[1][1] == 7
