"""
Email Alias Manager
===================

Unified interface for generating email aliases via different providers:
  - **burner**      – Claim disposable emails from the burner pool API
  - **faker**       – Offline fake emails (no API needed)
  - **simplelogin** – SimpleLogin custom alias API
  - **addy**        – Addy.io (AnonAddy) random alias API

Usage::

    from alias_manager import create_alias

    email = await create_alias("burner")
    email = await create_alias("simplelogin", first_name="John", last_name="Doe")
    email = await create_alias("addy")
    email = await create_alias("faker", first_name="John", last_name="Doe")
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Any
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("flare_bot")

# ---------------------------------------------------------------------------
# Burner email pool API
# ---------------------------------------------------------------------------

BURNER_API_BASE_ENV_VARS = (
    "BURNER_EMAIL_API_BASE_URL",
    "BURNER_EMAIL_BASE_URL",
    "EMAIL_BURNER_API_BASE_URL",
    "EMAIL_BURNER_API_URL",
    "BURNER_API_BASE_URL",
    "BURNER_API_URL",
)
BURNER_DEFAULT_API_BASE = (
    "http://explor-email-abkiynlw6f5g-1668049147.eu-west-1.elb.amazonaws.com"
)
BURNER_API_KEY_ENV_VARS = (
    "BURNER_EMAIL_API_KEY",
    "EMAIL_BURNER_API_KEY",
    "BURNER_API_KEY",
)
BURNER_TOKEN_ENV_VARS = (
    "BURNER_EMAIL_JWT_TOKEN",
    "BURNER_EMAIL_AUTH_TOKEN",
)
BURNER_USERNAME_ENV_VARS = (
    "BURNER_EMAIL_USERNAME",
    "EMAIL_BURNER_USERNAME",
    "BURNER_USERNAME",
)
BURNER_PASSWORD_ENV_VARS = (
    "BURNER_EMAIL_PASSWORD",
    "EMAIL_BURNER_PASSWORD",
    "BURNER_PASSWORD",
)
BURNER_SITE_ENV_VAR = "BURNER_EMAIL_SITE"
BURNER_TTL_ENV_VAR = "BURNER_EMAIL_TTL_DAYS"
BURNER_DEFAULT_SITE = "resa.notredamedeparis.fr"
_BURNER_SITE_CACHE: set[tuple[str, str, int | None]] = set()
_BURNER_TOKEN_CACHE: dict[tuple[str, str], str] = {}


def _get_first_env_value(*names: str) -> str:
    """Return the first non-empty environment variable value."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _get_burner_api_base(api_base: str | None = None) -> str:
    """Resolve the burner API base URL from explicit input or environment."""
    value = (
        api_base
        or _get_first_env_value(*BURNER_API_BASE_ENV_VARS)
        or BURNER_DEFAULT_API_BASE
    ).strip()
    return value.rstrip("/")


def _get_burner_token_from_env(token: str | None = None) -> str:
    """Resolve a direct bearer token from explicit input or environment."""
    return (token or _get_first_env_value(*BURNER_TOKEN_ENV_VARS)).strip()


def _get_burner_api_key(api_key: str | None = None) -> str:
    """Resolve a direct X-API-Key credential from explicit input or environment."""
    return (api_key or _get_first_env_value(*BURNER_API_KEY_ENV_VARS)).strip()


def _get_burner_username() -> str:
    """Resolve the burner API username from environment."""
    return _get_first_env_value(*BURNER_USERNAME_ENV_VARS)


def _get_burner_password() -> str:
    """Resolve the burner API password from environment."""
    return _get_first_env_value(*BURNER_PASSWORD_ENV_VARS)


def _get_burner_site_name(site_name: str | None = None) -> str:
    """Resolve the burner site name used for claims."""
    value = (site_name or os.getenv(BURNER_SITE_ENV_VAR, BURNER_DEFAULT_SITE)).strip()
    if not value:
        raise RuntimeError(f"{BURNER_SITE_ENV_VAR} cannot be empty")
    return value


def _get_burner_ttl_days(ttl_days: int | None = None) -> int | None:
    """Resolve an optional burner site TTL from explicit input or environment."""
    if ttl_days is not None:
        if ttl_days <= 0:
            raise RuntimeError("Burner site TTL must be a positive integer")
        return ttl_days

    raw_value = os.getenv(BURNER_TTL_ENV_VAR, "").strip()
    if not raw_value:
        return None

    try:
        parsed_value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{BURNER_TTL_ENV_VAR} must be a positive integer"
        ) from exc

    if parsed_value <= 0:
        raise RuntimeError(f"{BURNER_TTL_ENV_VAR} must be a positive integer")

    return parsed_value


def _build_burner_headers(
    *,
    api_key: str = "",
    token: str = "",
) -> dict[str, str]:
    """Build request headers for the burner email API."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _read_json_response(resp: aiohttp.ClientResponse) -> Any:
    """Read a JSON response while tolerating missing content-type headers."""
    return await resp.json(content_type=None)


def _build_burner_site_payload(site_name: str, ttl_days: int | None) -> dict[str, Any]:
    """Build a site payload compatible with both snake_case and camelCase fields."""
    payload: dict[str, Any] = {"name": site_name}
    if ttl_days is not None:
        payload["ttlDays"] = ttl_days
        payload["ttl_days"] = ttl_days
    return payload


def _build_burner_ttl_payload(ttl_days: int) -> dict[str, int]:
    """Build a TTL payload compatible with both snake_case and camelCase fields."""
    return {
        "ttlDays": ttl_days,
        "ttl_days": ttl_days,
    }


async def _list_burner_sites(
    http: aiohttp.ClientSession,
    *,
    api_base: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    """List burner sites to distinguish between duplicate-name and invalid-name errors."""
    async with http.get(
        f"{api_base}/api/sites",
        headers=headers,
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(
                f"Burner site list failed (HTTP {resp.status}): {body[:300]}"
            )
        data = await _read_json_response(resp)

    if not isinstance(data, list):
        raise RuntimeError(f"Burner site list returned unexpected payload: {data}")

    return [site for site in data if isinstance(site, dict)]


async def _login_burner(
    http: aiohttp.ClientSession,
    *,
    api_base: str,
    username: str,
    password: str,
) -> str:
    """Authenticate with the burner API and return a bearer token."""
    async with http.post(
        f"{api_base}/api/auth/login",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"username": username, "password": password},
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(
                f"Burner login failed (HTTP {resp.status}): {body[:300]}"
            )
        data = await _read_json_response(resp)

    token = str(data.get("token", "")).strip() if isinstance(data, dict) else ""
    if not token:
        raise RuntimeError(f"Burner login returned no token: {data}")

    return token


async def _get_burner_bearer_token(
    http: aiohttp.ClientSession,
    *,
    api_base: str,
    token: str | None = None,
) -> str:
    """Resolve a bearer token from env or by logging in with username/password."""
    direct_token = _get_burner_token_from_env(token)
    if direct_token:
        return direct_token

    username = _get_burner_username()
    password = _get_burner_password()
    if not username or not password:
        token_env_names = ", ".join(BURNER_TOKEN_ENV_VARS)
        username_env_names = ", ".join(BURNER_USERNAME_ENV_VARS)
        password_env_names = ", ".join(BURNER_PASSWORD_ENV_VARS)
        raise RuntimeError(
            "Burner auth is not configured. "
            f"Set one of these bearer token vars: {token_env_names}, "
            f"or set username/password vars: {username_env_names} + "
            f"{password_env_names}"
        )

    cache_key = (api_base, username)
    cached_token = _BURNER_TOKEN_CACHE.get(cache_key, "")
    if cached_token:
        return cached_token

    issued_token = await _login_burner(
        http,
        api_base=api_base,
        username=username,
        password=password,
    )
    _BURNER_TOKEN_CACHE[cache_key] = issued_token
    return issued_token


async def _ensure_burner_site(
    http: aiohttp.ClientSession,
    *,
    api_base: str,
    headers: dict[str, str],
    site_name: str,
    ttl_days: int | None,
) -> None:
    """Create the burner site if needed and optionally enforce its TTL."""
    cache_key = (api_base, site_name, ttl_days)
    if cache_key in _BURNER_SITE_CACHE:
        return

    create_payload = _build_burner_site_payload(site_name, ttl_days)

    async with http.post(
        f"{api_base}/api/sites",
        headers=headers,
        json=create_payload,
    ) as resp:
        body = await resp.text()
        if resp.status in {200, 201, 204, 409}:
            pass
        elif resp.status == 400:
            sites = await _list_burner_sites(
                http,
                api_base=api_base,
                headers=headers,
            )
            site_names = {
                str(site.get("name", "")).strip().lower()
                for site in sites
                if site.get("name")
            }
            if site_name.lower() not in site_names:
                raise RuntimeError(
                    f"Burner site create failed (HTTP {resp.status}): {body[:300]}"
                )
        else:
            raise RuntimeError(
                f"Burner site create failed (HTTP {resp.status}): {body[:300]}"
            )

    if ttl_days is not None:
        async with http.put(
            f"{api_base}/api/sites/{quote(site_name, safe='')}",
            headers=headers,
            json=_build_burner_ttl_payload(ttl_days),
        ) as resp:
            body = await resp.text()
            if resp.status not in {200, 204}:
                raise RuntimeError(
                    f"Burner site TTL update failed (HTTP {resp.status}): "
                    f"{body[:300]}"
                )

    _BURNER_SITE_CACHE.add(cache_key)


def _extract_claimed_emails(payload: Any) -> list[str]:
    """Extract claimed email strings from the burner API response."""
    root = payload.get("data", payload) if isinstance(payload, dict) else payload

    claimed: list[str] = []
    if isinstance(root, dict):
        raw_emails = root.get("emails")
        if isinstance(raw_emails, list):
            for item in raw_emails:
                if isinstance(item, str):
                    email = item.strip()
                elif isinstance(item, dict):
                    email = str(item.get("email", "")).strip()
                else:
                    email = ""
                if email:
                    claimed.append(email)

        single_email = root.get("email")
        if not claimed and isinstance(single_email, str) and single_email.strip():
            claimed.append(single_email.strip())

    if not claimed:
        raise RuntimeError(f"Burner claim returned no emails: {payload}")

    return claimed


async def claim_burner_emails(
    count: int,
    *,
    site_name: str | None = None,
    ttl_days: int | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    token: str | None = None,
) -> list[str]:
    """Claim one or more burner emails from the shared pool API."""
    if count <= 0:
        raise ValueError("count must be greater than zero")

    api_base = _get_burner_api_base(api_base)
    site_name = _get_burner_site_name(site_name)
    ttl_days = _get_burner_ttl_days(ttl_days)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as http:
        resolved_api_key = _get_burner_api_key(api_key)
        resolved_token = _get_burner_token_from_env(token)
        if resolved_api_key:
            headers = _build_burner_headers(api_key=resolved_api_key)
        else:
            if not resolved_token:
                resolved_token = await _get_burner_bearer_token(
                    http,
                    api_base=api_base,
                    token=token,
                )
            headers = _build_burner_headers(token=resolved_token)

        await _ensure_burner_site(
            http,
            api_base=api_base,
            headers=headers,
            site_name=site_name,
            ttl_days=ttl_days,
        )

        async with http.post(
            f"{api_base}/api/emails/claim",
            headers=headers,
            json={"count": count, "site": site_name},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"Burner claim failed (HTTP {resp.status}): {body[:300]}"
                )
            data = await _read_json_response(resp)

    claimed_emails = _extract_claimed_emails(data)
    if len(claimed_emails) < count:
        raise RuntimeError(
            f"Burner claim returned {len(claimed_emails)} email(s), "
            f"expected {count}: {data}"
        )

    logger.info(
        "Burner email(s) claimed: %d for site %s",
        len(claimed_emails),
        site_name,
    )
    return claimed_emails[:count]


# ---------------------------------------------------------------------------
# SimpleLogin
# ---------------------------------------------------------------------------

SIMPLELOGIN_API_BASE = "https://app.simplelogin.io/api"


async def _create_simplelogin_alias(
    first_name: str,
    last_name: str,
    api_key: str | None = None,
) -> str:
    """Create a SimpleLogin email alias with a *firstname.lastname* prefix.

    Uses the custom-alias endpoint so the resulting address looks like
    ``firstname.lastname.<suffix>@<domain>`` (dots only, no ``+`` or other
    special characters).

    Args:
        first_name: User's first name (used in alias prefix).
        last_name:  User's last name  (used in alias prefix).
        api_key:    SimpleLogin API key.  Falls back to env
                    ``SIMPLELOGIN_API_KEY``.

    Returns:
        The newly created alias email address.

    Raises:
        RuntimeError: On any API or network error.
    """
    api_key = api_key or os.getenv("SIMPLELOGIN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SIMPLELOGIN_API_KEY is not set")

    headers = {"Authentication": api_key, "Content-Type": "application/json"}

    # Build a clean prefix: lowercase, dots only, no special chars
    prefix = f"{first_name}.{last_name}".lower()
    prefix = re.sub(r"[^a-z0-9.]", "", prefix)
    # Collapse consecutive dots and strip leading/trailing dots
    prefix = re.sub(r"\.{2,}", ".", prefix).strip(".")
    # Add a short random suffix to avoid collisions across instances
    prefix += f".{random.randint(100, 9999)}"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as http:
        # 1) Fetch alias options to get a signed suffix
        async with http.get(
            f"{SIMPLELOGIN_API_BASE}/v5/alias/options",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"SimpleLogin alias/options failed (HTTP {resp.status}): {body[:300]}"
                )
            options = await resp.json(content_type=None)

        suffixes = options.get("suffixes", [])
        if not suffixes:
            raise RuntimeError("SimpleLogin returned no available suffixes")

        signed_suffix = suffixes[0]["signed_suffix"]

        # 2) Fetch the user's default mailbox ID
        async with http.get(
            f"{SIMPLELOGIN_API_BASE}/v2/mailboxes",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"SimpleLogin mailboxes fetch failed (HTTP {resp.status}): {body[:300]}"
                )
            mailbox_data = await resp.json(content_type=None)

        mailboxes = mailbox_data.get("mailboxes", [])
        if not mailboxes:
            raise RuntimeError("SimpleLogin returned no mailboxes")

        # Prefer the default mailbox; fall back to the first verified one
        default_mb = next(
            (m for m in mailboxes if m.get("default")),
            next((m for m in mailboxes if m.get("verified")), mailboxes[0]),
        )
        mailbox_id = default_mb["id"]

        # 3) Create the custom alias (with retry for rate limits)
        create_payload = {
            "alias_prefix": prefix,
            "signed_suffix": signed_suffix,
            "mailbox_ids": [mailbox_id],
            "note": "Auto-created by flare_bot",
        }

        max_retries = 5
        for attempt in range(max_retries):
            async with http.post(
                f"{SIMPLELOGIN_API_BASE}/v3/alias/custom/new",
                headers=headers,
                json=create_payload,
            ) as resp:
                body = await resp.text()
                if resp.status == 201:
                    alias_data = await resp.json(content_type=None)
                    break
                elif resp.status == 429:
                    wait = 2 ** attempt + random.uniform(0.5, 1.5)
                    logger.warning(
                        "SimpleLogin rate limited (attempt %d/%d), "
                        "retrying in %.1fs...",
                        attempt + 1, max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(
                        f"SimpleLogin create alias failed (HTTP {resp.status}): {body[:300]}"
                    )
        else:
            raise RuntimeError(
                "SimpleLogin rate limit: max retries exhausted"
            )

    alias_email = alias_data.get("email", "")
    if not alias_email:
        raise RuntimeError(f"SimpleLogin returned no email in response: {alias_data}")

    logger.info("SimpleLogin alias created: %s", alias_email)
    return alias_email


# ---------------------------------------------------------------------------
# Addy.io (AnonAddy)
# ---------------------------------------------------------------------------

ADDY_API_BASE = "https://app.addy.io/api/v1"


async def _create_addy_alias(
    api_key: str | None = None,
    domain: str | None = None,
) -> str:
    """Create a random Addy.io (AnonAddy) email alias.

    Uses the ``POST /api/v1/aliases`` endpoint with ``format=uuid`` so
    it works on the free plan (no custom local_part needed).

    Args:
        api_key: Addy.io API key.  Falls back to env ``ADDY_API_KEY``.
        domain:  Domain to use.  Falls back to env ``ADDY_DOMAIN``,
                 then defaults to ``anonaddy.me``.

    Returns:
        The newly created alias email address.

    Raises:
        RuntimeError: On any API or network error.
    """
    api_key = api_key or os.getenv("ADDY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ADDY_API_KEY is not set")

    domain = domain or os.getenv("ADDY_DOMAIN", "anonaddy.me").strip()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }

    payload = {
        "domain": domain,
        "format": "uuid",
        "description": "Auto-created by flare_bot",
    }

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as http:
        max_retries = 5
        for attempt in range(max_retries):
            async with http.post(
                f"{ADDY_API_BASE}/aliases",
                headers=headers,
                json=payload,
            ) as resp:
                body = await resp.text()
                if resp.status == 201:
                    data = await resp.json(content_type=None)
                    break
                elif resp.status == 429:
                    wait = 2 ** attempt + random.uniform(0.5, 1.5)
                    logger.warning(
                        "Addy.io rate limited (attempt %d/%d), "
                        "retrying in %.1fs...",
                        attempt + 1, max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Addy.io create alias failed (HTTP {resp.status}): {body[:300]}"
                    )
        else:
            raise RuntimeError("Addy.io rate limit: max retries exhausted")

    alias_email = data.get("data", {}).get("email", "")
    if not alias_email:
        raise RuntimeError(f"Addy.io returned no email in response: {data}")

    logger.info("Addy.io alias created: %s", alias_email)
    return alias_email


# ---------------------------------------------------------------------------
# Faker (offline)
# ---------------------------------------------------------------------------


def _create_faker_email(first_name: str, last_name: str) -> str:
    """Generate a fake Gmail-style email address using Faker names.

    No API call — fully offline.
    """
    email = (
        f"{first_name}{last_name}{random.randint(500, 9999)}k@gmail.com".lower()
    )
    logger.info("Faker email generated: %s", email)
    return email


# ---------------------------------------------------------------------------
# Unified interface
# ---------------------------------------------------------------------------

VALID_PROVIDERS = ("burner", "faker", "simplelogin", "addy")


async def create_alias(
    provider: str,
    *,
    first_name: str = "",
    last_name: str = "",
) -> str:
    """Create an email alias using the specified provider.

    Args:
        provider:   One of ``'burner'``, ``'faker'``, ``'simplelogin'``,
                    ``'addy'``.
        first_name: Used by ``faker`` and ``simplelogin`` providers.
        last_name:  Used by ``faker`` and ``simplelogin`` providers.

    Returns:
        The generated / created email address.

    Raises:
        ValueError:  If provider is not recognised.
        RuntimeError: On API errors.
    """
    provider = provider.lower().strip()

    if provider == "burner":
        return (await claim_burner_emails(1))[0]
    elif provider == "faker":
        return _create_faker_email(first_name, last_name)
    elif provider == "simplelogin":
        return await _create_simplelogin_alias(first_name, last_name)
    elif provider == "addy":
        return await _create_addy_alias()
    else:
        raise ValueError(
            f"Unknown email provider '{provider}'. "
            f"Must be one of: {', '.join(VALID_PROVIDERS)}"
        )
