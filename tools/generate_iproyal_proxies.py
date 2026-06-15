from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


SESSION_RE = re.compile(r"_session-[A-Za-z0-9]{8}")
LIFETIME_RE = re.compile(r"_lifetime-[^_]+")
COUNTRY_RE = re.compile(r"_country-[^_]+")
TOKEN_CHARS = string.ascii_letters + string.digits


@dataclass(frozen=True)
class ProxyTemplate:
    host: str
    port: str
    username: str
    password: str


def parse_proxy(raw_proxy: str) -> ProxyTemplate:
    raw_proxy = raw_proxy.strip().strip("[]")
    if raw_proxy.startswith("http://"):
        raw_proxy = raw_proxy.removeprefix("http://")
    elif raw_proxy.startswith("https://"):
        raw_proxy = raw_proxy.removeprefix("https://")

    if "@" in raw_proxy:
        credentials, endpoint = raw_proxy.rsplit("@", 1)
        username, password = credentials.split(":", 1)
        host, port = endpoint.split(":", 1)
        return ProxyTemplate(host, port, username, password)

    parts = raw_proxy.split(":", 3)
    if len(parts) != 4:
        raise ValueError("Proxy must be HOST:PORT:USERNAME:PASSWORD")
    return ProxyTemplate(*parts)


def random_session_token() -> str:
    return "".join(random.choice(TOKEN_CHARS) for _ in range(8))


def build_password(
    password_template: str,
    *,
    session_token: str,
    lifetime: str,
    country: str,
) -> str:
    password = SESSION_RE.sub("", password_template)
    password = LIFETIME_RE.sub("", password)
    if country:
        password = COUNTRY_RE.sub("", password)
        password = f"{password}_country-{country.lower()}"
    return f"{password}_session-{session_token}_lifetime-{lifetime}"


def generate_proxies(
    proxy_template: ProxyTemplate,
    *,
    count: int,
    lifetime: str,
    country: str,
) -> list[str]:
    proxies: list[str] = []
    seen_tokens: set[str] = set()

    while len(proxies) < count:
        token = random_session_token()
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        password = build_password(
            proxy_template.password,
            session_token=token,
            lifetime=lifetime,
            country=country,
        )
        proxies.append(
            f"{proxy_template.host}:{proxy_template.port}:"
            f"{proxy_template.username}:{password}"
        )

    return proxies


def session_token(proxy: str) -> str:
    match = SESSION_RE.search(parse_proxy(proxy).password)
    return match.group(0).removeprefix("_session-") if match else "unknown"


def requests_proxy_url(proxy: str) -> str:
    parsed = parse_proxy(proxy)
    username = quote(parsed.username, safe="")
    password = quote(parsed.password, safe="")
    return f"http://{username}:{password}@{parsed.host}:{parsed.port}"


def fetch_exit_ip(
    proxy: str,
    *,
    timeout: float,
    target_url: str,
) -> tuple[str, str]:
    import requests

    try:
        proxy_url = requests_proxy_url(proxy)
        response = requests.get(
            target_url,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
        )
        response.raise_for_status()
        return str(response.json().get("ip", "")), ""
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return "", type(exc).__name__


def print_verify_result(
    *,
    index: int,
    proxy: str,
    ip_results: list[str],
    errors: list[str],
) -> None:
    unique_ips = sorted({ip for ip in ip_results if ip})
    print(
        json.dumps(
            {
                "proxy_index": index,
                "session": session_token(proxy),
                "requests": len(ip_results),
                "unique_ips": unique_ips,
                "stable": len(unique_ips) == 1 and not errors,
                "errors": errors,
            },
            sort_keys=True,
        )
    )


def verify_proxies(
    proxies: list[str],
    *,
    limit: int,
    attempts: int,
    timeout: float,
    target_url: str,
    concurrent: bool,
    warmup: bool,
) -> int:
    exit_code = 0

    if concurrent:
        selected = proxies[:limit]
        ip_results: dict[int, list[str]] = defaultdict(list)
        errors: dict[int, list[str]] = defaultdict(list)

        if warmup:
            for index, proxy in enumerate(selected, start=1):
                ip, error = fetch_exit_ip(
                    proxy,
                    timeout=timeout,
                    target_url=target_url,
                )
                if ip:
                    ip_results[index].append(ip)
                if error:
                    errors[index].append(f"warmup:{error}")
                    exit_code = 1

        with ThreadPoolExecutor(max_workers=max(1, min(limit * attempts, 32))) as pool:
            futures = {
                pool.submit(
                    fetch_exit_ip,
                    proxy,
                    timeout=timeout,
                    target_url=target_url,
                ): (index, proxy)
                for index, proxy in enumerate(selected, start=1)
                for _attempt in range(attempts)
            }
            for future in as_completed(futures):
                index, _proxy = futures[future]
                ip, error = future.result()
                if ip:
                    ip_results[index].append(ip)
                if error:
                    errors[index].append(error)
                    exit_code = 1

        for index, proxy in enumerate(selected, start=1):
            print_verify_result(
                index=index,
                proxy=proxy,
                ip_results=ip_results[index],
                errors=errors[index],
            )
        return exit_code

    for index, proxy in enumerate(proxies[:limit], start=1):
        ip_results = []
        errors = []

        for _attempt in range(attempts):
            ip, error = fetch_exit_ip(
                proxy,
                timeout=timeout,
                target_url=target_url,
            )
            if ip:
                ip_results.append(ip)
            if error:
                errors.append(error)
                exit_code = 1

        print_verify_result(
            index=index,
            proxy=proxy,
            ip_results=ip_results,
            errors=errors,
        )

    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate IPRoyal residential sticky-session proxy strings.",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("IPROYAL_PROXY", ""),
        help="Base proxy in HOST:PORT:USERNAME:PASSWORD format. "
        "Defaults to IPROYAL_PROXY.",
    )
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--country", default="us")
    parser.add_argument("--lifetime", default="24h")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verify-concurrent", action="store_true")
    parser.add_argument("--verify-warmup", action="store_true")
    parser.add_argument("--verify-limit", type=int, default=3)
    parser.add_argument("--verify-attempts", type=int, default=3)
    parser.add_argument("--verify-timeout", type=float, default=20)
    parser.add_argument(
        "--verify-url",
        default="https://api.ipify.org?format=json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.proxy:
        raise SystemExit("Provide --proxy or set IPROYAL_PROXY.")
    if args.count < 1:
        raise SystemExit("--count must be at least 1.")

    proxies = generate_proxies(
        parse_proxy(args.proxy),
        count=args.count,
        lifetime=args.lifetime,
        country=args.country,
    )

    if args.output:
        args.output.write_text("\n".join(proxies) + "\n", encoding="utf-8")
    elif not args.verify and not args.verify_concurrent:
        sys.stdout.write("\n".join(proxies) + "\n")

    if args.verify or args.verify_concurrent:
        return verify_proxies(
            proxies,
            limit=args.verify_limit,
            attempts=args.verify_attempts,
            timeout=args.verify_timeout,
            target_url=args.verify_url,
            concurrent=args.verify_concurrent,
            warmup=args.verify_warmup,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
