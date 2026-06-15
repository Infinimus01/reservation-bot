"""
FlareSolverr client module.

Provides both sync and async functions to send requests through a local
FlareSolverr instance with proxy support. Includes structured logging with
timestamped log files.

Usage (sync)::

    from flare import solve_flare

    result = solve_flare()
    print(result.solution.cookies)      # list[FlareCookie]
    print(result.solution.user_agent)   # str
    print(result.solution.cf_clearance) # str | None

Usage (async)::

    from flare import solve_flare_async

    result = await solve_flare_async()
    print(result.solution.url)
    for cookie in result.solution.cookies:
        print(cookie.name, cookie.value)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import aiohttp
import requests

from shared.config import (
    LEGACY_DEFAULT_FLARESOLVERR_URLS,
    get_flaresolverr_urls as resolve_flaresolverr_urls,
)

# ---------------------------------------------------------------------------
# Constants / Defaults
# ---------------------------------------------------------------------------

# Backward-compatible local-dev fallback pool.
DEFAULT_FLARESOLVERR_URLS: List[str] = list(LEGACY_DEFAULT_FLARESOLVERR_URLS)

# Keep a single-URL alias for backward compatibility.
DEFAULT_FLARESOLVERR_URL = DEFAULT_FLARESOLVERR_URLS[0]
DEFAULT_TARGET_URL = (
    "https://resa.notredamedeparis.fr/en/reservationindividuelle/date"
)
DEFAULT_UPSTREAM_PROXY = (
    "geo.iproyal.com:12321"
    "KaJnsmA32SGCWhzU"
    "sa68A7xvj0LaL6b6_country-us_city-abilene_session-fRBeSchy_lifetime-24h"
)
DEFAULT_MAX_TIMEOUT = 180000  # milliseconds

_flaresolverr_cycle_pool: tuple[str, ...] = tuple()
_flaresolverr_url_cycle = itertools.cycle(DEFAULT_FLARESOLVERR_URLS)


def get_flaresolverr_urls(
    explicit_url: str | None = None,
    explicit_urls: Sequence[str] | None = None,
) -> list[str]:
    """Resolve the runtime FlareSolverr URL pool.

    Resolution order:
    1. Explicit single URL
    2. Explicit URL list
    3. ``FLARESOLVERR_URLS``
    4. Generated local URLs from host/base-port/count env vars
    5. Legacy localhost fallback pool
    """
    return resolve_flaresolverr_urls(
        explicit_url=explicit_url,
        explicit_urls=explicit_urls,
    )


def get_default_flaresolverr_url() -> str:
    urls = get_flaresolverr_urls()
    if not urls:
        raise RuntimeError("No FlareSolverr URLs could be resolved")
    return urls[0]


def _next_flaresolverr_url(urls: Sequence[str] | None = None) -> str:
    """Return the next FlareSolverr URL from the runtime pool.

    This remains a local fallback helper only. Distributed scheduling should
    inject explicit URLs per worker/task instead of relying on module-global
    iterator state.
    """
    global _flaresolverr_cycle_pool
    global _flaresolverr_url_cycle

    pool = tuple(urls or get_flaresolverr_urls())
    if not pool:
        raise RuntimeError("No FlareSolverr URLs available")

    if pool != _flaresolverr_cycle_pool:
        _flaresolverr_cycle_pool = pool
        _flaresolverr_url_cycle = itertools.cycle(pool)

    return next(_flaresolverr_url_cycle)


LOGS_DIR = Path(__file__).resolve().parent / "logs"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logger(name: str = "flare") -> logging.Logger:
    """
    Create a logger that writes to both **console** and a **timestamped log file**.

    Log file naming example:
        logs/flare_2026-02-13_12-05-11.log

    The file contains DEBUG-level detail; the console shows INFO and above.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers when the module is re-imported
    if logger.handlers:
        return logger

    # ── Console handler (INFO+) ──────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)

    # ── File handler (DEBUG+) ────────────────────────────────────────────
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOGS_DIR / f"flare_{timestamp}.log"

    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    logger.debug("Logger initialised – log file: %s", log_file)
    return logger


logger = _setup_logger()

# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class FlareCookie:
    """A single cookie returned by FlareSolverr."""

    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expiry: Optional[int] = None
    http_only: bool = False
    secure: bool = False
    same_site: str = "Lax"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlareCookie:
        return cls(
            name=data.get("name", ""),
            value=data.get("value", ""),
            domain=data.get("domain", ""),
            path=data.get("path", "/"),
            expiry=data.get("expiry"),
            http_only=data.get("httpOnly", False),
            secure=data.get("secure", False),
            same_site=data.get("sameSite", "Lax"),
        )

    def __repr__(self) -> str:
        val = self.value[:30] + "…" if len(self.value) > 30 else self.value
        return f"FlareCookie({self.name}={val}, domain={self.domain})"

    # Playwright expects exact capitalization for sameSite values.
    _SAME_SITE_MAP = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
    }

    def to_dict(self, url: str = "") -> Dict[str, Any]:
        """Convert back to the dict format Playwright ``context.add_cookies`` expects.

        Playwright requires each cookie to have **either** a ``url`` (from
        which domain + path are inferred) **or** an explicit ``domain``.
        When both are provided, ``domain`` takes precedence.

        Args:
            url: Target URL used as fallback when the cookie has no domain.
        """
        # Normalise sameSite to the exact casing Playwright requires
        normalised_ss = self._SAME_SITE_MAP.get(
            self.same_site.lower(), "Lax"
        )

        has_domain = bool(self.domain and self.domain.strip())

        d: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "path": self.path if self.path else "/",
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": normalised_ss,
        }

        if self.expiry is not None:
            d["expires"] = self.expiry

        # Provide domain when we have it, url as fallback, both when possible.
        if has_domain:
            d["domain"] = self.domain
        if url:
            d["url"] = url
        if not has_domain and not url:
            raise ValueError(
                f"Cookie '{self.name}' has no domain and no url was provided"
            )
        return d


@dataclass(repr=False)
class FlareSolution:
    """The ``solution`` block from a FlareSolverr response."""

    url: str = ""
    status: int = 0
    cookies: List[FlareCookie] = field(default_factory=list)
    user_agent: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    response: str = ""  # raw HTML body

    # ── Convenience properties ───────────────────────────────────────────

    @property
    def cf_clearance(self) -> Optional[str]:
        """Return the ``cf_clearance`` cookie value, or *None*."""
        for c in self.cookies:
            if c.name == "cf_clearance":
                return c.value
        return None

    @property
    def session_cookie(self) -> Optional[FlareCookie]:
        """Return the PHP session cookie (``GTPHPSESSID``), or *None*."""
        for c in self.cookies:
            if c.name == "GTPHPSESSID":
                return c
        return None

    def cookies_as_dicts(self, url: str = "") -> List[Dict[str, Any]]:
        """Return cookies in raw dict form (for Playwright ``add_cookies``).

        Args:
            url: Target URL passed through to each cookie's ``to_dict``.
                 Ensures Playwright can always bind the cookie to the right
                 domain even if FlareSolverr omitted the domain field.
        """
        return [c.to_dict(url=url or self.url) for c in self.cookies]

    def __repr__(self) -> str:
        cookie_names = [c.name for c in self.cookies]
        html_len = len(self.response)
        return (
            f"FlareSolution(\n"
            f"  url={self.url},\n"
            f"  status={self.status},\n"
            f"  user_agent={self.user_agent[:80]}…,\n"
            f"  cookies={cookie_names},\n"
            f"  cf_clearance={'yes' if self.cf_clearance else 'no'},\n"
            f"  response=<{html_len} chars HTML>\n"
            f")"
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlareSolution:
        return cls(
            url=data.get("url", ""),
            status=data.get("status", 0),
            cookies=[
                FlareCookie.from_dict(c) for c in data.get("cookies", [])
            ],
            user_agent=data.get("userAgent", ""),
            headers=data.get("headers", {}),
            response=data.get("response", ""),
        )


@dataclass(repr=False)
class FlareResponse:
    """Typed wrapper for the full FlareSolverr JSON response."""

    status: str = ""  # "ok" or "error"
    message: str = ""
    solution: FlareSolution = field(default_factory=FlareSolution)
    start_timestamp: int = 0
    end_timestamp: int = 0
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def elapsed_ms(self) -> int:
        """Wall-clock time the challenge took (milliseconds)."""
        return self.end_timestamp - self.start_timestamp

    def __repr__(self) -> str:
        return (
            f"FlareResponse(\n"
            f"  status={self.status!r},\n"
            f"  message={self.message!r},\n"
            f"  elapsed={self.elapsed_ms}ms,\n"
            f"  version={self.version!r},\n"
            f"  solution={self.solution}\n"
            f")"
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlareResponse:
        solution_data = data.get("solution", {})
        return cls(
            status=data.get("status", ""),
            message=data.get("message", ""),
            solution=FlareSolution.from_dict(solution_data),
            start_timestamp=data.get("startTimestamp", 0),
            end_timestamp=data.get("endTimestamp", 0),
            version=data.get("version", ""),
        )


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------


def _parse_proxy(proxy_string: str) -> dict:
    """
    Parse an upstream proxy string of the form
        ``host:port:username:password``
    into the dict structure expected by FlareSolverr.
    """
    parts = proxy_string.split(":")
    if len(parts) < 4:
        raise ValueError(
            f"Invalid proxy format (expected host:port:user:pass): {proxy_string}"
        )

    host, port, username, password = parts[0], parts[1], parts[2], parts[3]
    return {
        "url": f"http://{host}:{port}",
        "username": username,
        "password": password,
    }


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def solve_flare(
    target_url: str = DEFAULT_TARGET_URL,
    flaresolverr_url: str | None = None,
    upstream_proxy: str = DEFAULT_UPSTREAM_PROXY,
    max_timeout: int = DEFAULT_MAX_TIMEOUT,
    cmd: str = "request.get",
    session: Optional[str] = None,
) -> FlareResponse:
    """
    Send a request through a local FlareSolverr instance.

    Args:
        target_url:       The URL to fetch via FlareSolverr.
        flaresolverr_url: Base URL of the FlareSolverr API.
        upstream_proxy:   Proxy string in ``host:port:user:pass`` format.
        max_timeout:      Maximum wait time in **milliseconds**.
        cmd:              FlareSolverr command (default ``request.get``).
        session:          Optional FlareSolverr session ID for cookie persistence.

    Returns:
        A :class:`FlareResponse` with typed access to cookies, user-agent, etc.

    Raises:
        requests.RequestException: On any HTTP / connection error.
        ValueError:                If the proxy string is malformed.
    """
    if flaresolverr_url is None:
        flaresolverr_url = get_default_flaresolverr_url()

    logger.info("─── FlareSolverr request ───────────────────────────────────")
    logger.info("Target URL   : %s", target_url)
    logger.info("Flare URL    : %s", flaresolverr_url)
    logger.info("Max timeout  : %d ms", max_timeout)
    logger.debug("Upstream proxy : %s", upstream_proxy)

    # Build proxy config
    proxy_config = _parse_proxy(upstream_proxy)
    logger.debug(
        "Parsed proxy → url=%s, user=%s",
        proxy_config["url"],
        proxy_config["username"],
    )

    # Build payload
    payload: dict = {
        "cmd": cmd,
        "url": target_url,
        "maxTimeout": max_timeout,
        "returnScreenshot":True,
        "proxy": proxy_config,
        "waitInSeconds": 30,
    }
    if session:
        payload["session"] = session
        logger.debug("Using session: %s", session)

    headers = {"Content-Type": "application/json"}

    # Execute request
    logger.info("Sending request to FlareSolverr …")
    try:
        response = requests.post(
            flaresolverr_url,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        logger.info(
            "Response received – status=%d, length=%d bytes",
            response.status_code,
            len(response.content),
        )
        logger.debug("Response body (first 500 chars): %s", response.text[:500])

        result = FlareResponse.from_dict(response.json())
        logger.info(
            "Challenge %s – %s (took %d ms, user_agent=%s)",
            "solved" if result.ok else "FAILED",
            result.message,
            result.elapsed_ms,
            result.solution.user_agent[:60],
        )
        return result

    except requests.ConnectionError:
        logger.error(
            "Connection failed – is FlareSolverr running at %s?",
            flaresolverr_url,
        )
        raise
    except requests.Timeout:
        logger.error("Request timed out after %d ms", max_timeout)
        raise
    except requests.HTTPError as exc:
        logger.error("HTTP error: %s", exc)
        raise
    except requests.RequestException as exc:
        logger.error("Unexpected request error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------


async def solve_flare_async(
    target_url: str = DEFAULT_TARGET_URL,
    flaresolverr_url: Optional[str] = None,
    upstream_proxy: str = DEFAULT_UPSTREAM_PROXY,
    max_timeout: int = DEFAULT_MAX_TIMEOUT,
    cmd: str = "request.get",
    session: Optional[str] = None,
) -> FlareResponse:
    """
    Async version of :func:`solve_flare` — non-blocking, suitable for use
    inside ``asyncio`` / Playwright flows.

    Uses :mod:`aiohttp` under the hood. When *flaresolverr_url* is ``None``
    (the default), the next URL from the resolved runtime pool is chosen
    automatically in round-robin order.

    Args:
        target_url:       The URL to fetch via FlareSolverr.
        flaresolverr_url: Base URL of the FlareSolverr API.  ``None`` to
                          use the round-robin pool.
        upstream_proxy:   Proxy string in ``host:port:user:pass`` format.
        max_timeout:      Maximum wait time in **milliseconds**.
        cmd:              FlareSolverr command (default ``request.get``).
        session:          Optional FlareSolverr session ID for cookie persistence.

    Returns:
        A :class:`FlareResponse` with typed access to cookies, user-agent, etc.

    Raises:
        aiohttp.ClientError: On any HTTP / connection error.
        ValueError:          If the proxy string is malformed.
    """
    # Pick the next URL from the round-robin pool when none is specified.
    if flaresolverr_url is None:
        flaresolverr_url = _next_flaresolverr_url()

    logger.info("─── FlareSolverr async request ─────────────────────────────")
    logger.info("Target URL   : %s", target_url)
    logger.info("Flare URL    : %s", flaresolverr_url)
    logger.info("Max timeout  : %d ms", max_timeout)
    # Log full proxy string (with session ID visible) so we can verify
    # the browser uses the exact same session for IP stickiness.
    logger.info("Upstream proxy: %s", upstream_proxy)

    # Build proxy config
    proxy_config = _parse_proxy(upstream_proxy)
    logger.debug(
        "Parsed proxy → url=%s, user=%s",
        proxy_config["url"],
        proxy_config["username"],
    )

    # Build payload
    payload: dict = {
        "cmd": cmd,
        "url": target_url,
        "maxTimeout": max_timeout,
        "proxy": proxy_config,
        # "waitInSeconds": 60,
        "returnScreenshot":False,
    }
    if session:
        payload["session"] = session
        logger.debug("Using session: %s", session)

    headers = {"Content-Type": "application/json"}

    # Convert max_timeout from ms → seconds for the HTTP client timeout
    timeout = aiohttp.ClientTimeout(total=max_timeout / 1000 + 30)  # extra 30s buffer

    logger.info("Sending async request to FlareSolverr …")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with http_session.post(
                flaresolverr_url,
                headers=headers,
                json=payload,
            ) as resp:
                body = await resp.text()
                status = resp.status

                # Try to parse JSON
                json_body = None
                try:
                    json_body = await resp.json(content_type=None)
                except Exception:
                    pass

                if status >= 400:
                    logger.error(
                        "HTTP error %d from FlareSolverr: %s",
                        status,
                        body[:500],
                    )
                    resp.raise_for_status()

                logger.info(
                    "Response received – status=%d, length=%d bytes",
                    status,
                    len(body),
                )
                logger.debug("Response body (first 500 chars): %s", body[:500])

                result = FlareResponse.from_dict(json_body or {})
                logger.info(
                    "Challenge %s – %s (took %d ms, user_agent=%s)",
                    "solved" if result.ok else "FAILED",
                    result.message,
                    result.elapsed_ms,
                    result.solution.user_agent[:60],
                )

                # ── Detailed cookie diagnostics for CF bypass debugging ──
                logger.info(
                    "FlareSolverr returned %d cookie(s), cf_clearance=%s",
                    len(result.solution.cookies),
                    "PRESENT" if result.solution.cf_clearance else "MISSING",
                )
                for c in result.solution.cookies:
                    expiry_str = "session" if c.expiry is None else str(c.expiry)
                    if isinstance(c.expiry, (int, float)) and c.expiry > 0:
                        remaining = c.expiry - _time.time()
                        expiry_str += f" (expires in {remaining:.0f}s)"
                        if remaining < 0:
                            expiry_str += " ⚠ ALREADY EXPIRED"
                    logger.debug(
                        "  cookie: %-30s domain=%-35s path=%-5s "
                        "sameSite=%-8s secure=%-5s httpOnly=%-5s expiry=%s",
                        c.name,
                        c.domain,
                        c.path,
                        c.same_site,
                        c.secure,
                        c.http_only,
                        expiry_str,
                    )
                    logger.debug(
                        "    value[:%d]: %s",
                        min(60, len(c.value)),
                        c.value[:60],
                    )

                # Log the proxy that FlareSolverr used so we can compare IPs
                logger.info(
                    "FlareSolverr proxy: %s:%s (user=%s, session in URL=%s)",
                    proxy_config["url"].split("//")[-1].split(":")[0],
                    proxy_config["url"].split(":")[-1],
                    proxy_config["username"][:12] + "…",
                    "session" in proxy_config.get("username", "").lower()
                    or "session" in proxy_config.get("password", "").lower(),
                )

                # Verify the proxy's exit IP at solve time so we can
                # compare with the browser's IP later in main_.py.
                try:
                    _parts = upstream_proxy.split(":")
                    _h, _po, _u = _parts[0], _parts[1], _parts[2]
                    _pw = ":".join(_parts[3:])
                    _px = f"http://{_u}:{_pw}@{_h}:{_po}"
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as _ip_sess:
                        async with _ip_sess.get(
                            "https://api.ipify.org?format=json",
                            proxy=_px,
                            ssl=False,
                        ) as _ip_r:
                            _ip_j = await _ip_r.json(content_type=None)
                            logger.info(
                                "FlareSolverr proxy exit IP at solve time: %s",
                                _ip_j.get("ip", "unknown"),
                            )
                except Exception as _ip_err:
                    logger.warning(
                        "Could not verify FlareSolverr proxy exit IP: %s", _ip_err
                    )

                return result

    except aiohttp.ClientConnectorError:
        logger.error(
            "Connection failed – is FlareSolverr running at %s?",
            flaresolverr_url,
        )
        raise
    except asyncio.TimeoutError:
        logger.error("Async request timed out after %d ms", max_timeout)
        raise
    except aiohttp.ClientResponseError as exc:
        logger.error("HTTP error: %s", exc)
        raise
    except aiohttp.ClientError as exc:
        logger.error("Unexpected async request error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# CLI entry-point (preserved for standalone usage)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio as _asyncio

    # Sync version
    print("=== Sync ===")
    result = solve_flare()
    print(f"Status : {result.status}")
    print(f"Message: {result.message}")
    print(f"URL    : {result.solution.url}")
    print(f"UA     : {result.solution.user_agent}")
    print(f"CF clr : {result.solution.cf_clearance}")
    print(f"Cookies: {len(result.solution.cookies)}")
    for c in result.solution.cookies:
        print(f"  {c.name} = {c.value[:40]}...")

    # # Async version
    # print("\n=== Async ===")
    # result = _asyncio.run(solve_flare_async())
    # print(f"Status: {result.status}, URL: {result.solution.url}")
