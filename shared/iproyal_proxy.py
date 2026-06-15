from __future__ import annotations

import logging
import os
import re
import secrets
import string
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass


IPROYAL_SESSION_RE = re.compile(r"_session-[A-Za-z0-9]{8}")
IPROYAL_LIFETIME_RE = re.compile(r"_lifetime-[^_]+")
IPROYAL_COUNTRY_RE = re.compile(r"_country-[^_]+")
IPROYAL_SESSION_CHARS = string.ascii_letters + string.digits


@dataclass(frozen=True)
class IPRoyalProxySettings:
    base_proxy: str
    country: str
    lifetime: str
    warmup_enabled: bool
    warmup_attempts: int
    warmup_timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.base_proxy)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> IPRoyalProxySettings:
        runtime_env = env if env is not None else os.environ
        return cls(
            base_proxy=runtime_env.get("IPROYAL_PROXY", "").strip(),
            country=runtime_env.get("IPROYAL_PROXY_COUNTRY", "us").strip(),
            lifetime=runtime_env.get("IPROYAL_PROXY_LIFETIME", "24h").strip(),
            warmup_enabled=_env_bool(
                runtime_env.get("IPROYAL_PROXY_WARMUP"),
                default=True,
            ),
            warmup_attempts=max(
                1,
                int(runtime_env.get("IPROYAL_PROXY_WARMUP_ATTEMPTS", "2")),
            ),
            warmup_timeout_seconds=float(
                runtime_env.get("IPROYAL_PROXY_WARMUP_TIMEOUT_SECONDS", "20")
            ),
        )


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _session_token() -> str:
    return "".join(secrets.choice(IPROYAL_SESSION_CHARS) for _ in range(8))


def build_iproyal_sticky_proxy(
    base_proxy: str,
    *,
    country: str,
    lifetime: str,
) -> str:
    parts = base_proxy.strip().split(":", 3)
    if len(parts) != 4:
        raise ValueError("IPROYAL_PROXY must use HOST:PORT:USERNAME:PASSWORD format")

    host, port, username, password = parts
    password = IPROYAL_SESSION_RE.sub("", password)
    password = IPROYAL_LIFETIME_RE.sub("", password)

    country = country.strip().lower()
    if country:
        password = IPROYAL_COUNTRY_RE.sub("", password)
        password = f"{password}_country-{country}"

    lifetime = lifetime.strip() or "24h"
    password = f"{password}_session-{_session_token()}_lifetime-{lifetime}"
    return f"{host}:{port}:{username}:{password}"


def proxy_display(proxy_value: str) -> str:
    if not proxy_value:
        return ""

    parts = proxy_value.split(":")
    if len(parts) < 4:
        return parts[0] + ":***"

    display = ":".join(parts[:3]) + ":***"
    password = ":".join(parts[3:])
    session_match = IPROYAL_SESSION_RE.search(password)
    lifetime_match = IPROYAL_LIFETIME_RE.search(password)
    country_match = IPROYAL_COUNTRY_RE.search(password)

    details: list[str] = []
    if session_match:
        details.append(f"session={session_match.group(0).removeprefix('_session-')}")
    if country_match:
        details.append(f"country={country_match.group(0).removeprefix('_country-')}")
    if lifetime_match:
        details.append(f"lifetime={lifetime_match.group(0).removeprefix('_lifetime-')}")

    if details:
        display = f"{display} ({', '.join(details)})"
    return display


async def acquire_warmed_iproyal_proxy(
    settings: IPRoyalProxySettings,
    *,
    warmup: Callable[[str, float], Awaitable[bool]],
    logger: logging.Logger,
    context: str,
) -> str:
    if not settings.enabled:
        return ""

    for attempt in range(1, settings.warmup_attempts + 1):
        proxy = build_iproyal_sticky_proxy(
            settings.base_proxy,
            country=settings.country,
            lifetime=settings.lifetime,
        )
        display = proxy_display(proxy)
        if not settings.warmup_enabled:
            logger.info("%s using generated IPRoyal sticky proxy %s", context, display)
            return proxy

        if await warmup(proxy, settings.warmup_timeout_seconds):
            logger.info("%s using warmed IPRoyal sticky proxy %s", context, display)
            return proxy

        logger.warning(
            "%s IPRoyal sticky proxy warm-up failed for %s (attempt %d/%d)",
            context,
            display,
            attempt,
            settings.warmup_attempts,
        )

    raise RuntimeError("Could not warm up a generated IPRoyal sticky proxy")
