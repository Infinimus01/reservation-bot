from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from typing import Iterable

import aiohttp

from shared.config import (
    LEGACY_DEFAULT_FLARESOLVERR_URLS,
    WorkerSettings,
    build_local_flaresolverr_urls,
    split_csv_urls,
)


logger = logging.getLogger("worker.flaresolverr_pool")


class FlaresolverrPool:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings

    async def get_healthy_urls(self) -> list[str]:
        candidate_urls = await self._discover_urls()
        if not candidate_urls:
            return []
        return await self._healthy_urls(candidate_urls)

    async def _healthy_urls(self, candidate_urls: list[str]) -> list[str]:
        checks = await asyncio.gather(
            *(self._is_healthy(url) for url in candidate_urls),
            return_exceptions=True,
        )
        healthy_urls: list[str] = []
        for url, result in zip(candidate_urls, checks):
            if result is True:
                healthy_urls.append(url)
        return healthy_urls

    async def wait_for_healthy_urls(
        self,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> list[str]:
        deadline = time.monotonic() + max(timeout_seconds, 0)
        best_healthy_urls: list[str] = []
        while True:
            candidate_urls, source = await self._discover_url_candidates()
            expected_count = self._expected_healthy_count(candidate_urls, source)
            healthy_urls = await self._healthy_urls(candidate_urls)
            if len(healthy_urls) > len(best_healthy_urls):
                best_healthy_urls = healthy_urls

            if healthy_urls and len(healthy_urls) >= expected_count:
                logger.info(
                    "FlareSolverr pool ready with %d/%d healthy backend(s)",
                    len(healthy_urls),
                    expected_count,
                )
                return healthy_urls

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if best_healthy_urls:
                    logger.warning(
                        "FlareSolverr startup timeout reached; proceeding with "
                        "%d/%d healthy backend(s)",
                        len(best_healthy_urls),
                        expected_count,
                    )
                return best_healthy_urls

            logger.info(
                "Waiting for FlareSolverr backends to become healthy "
                "(%d/%d ready, %.0fs remaining)",
                len(healthy_urls),
                expected_count,
                remaining,
            )
            await asyncio.sleep(max(min(poll_seconds, remaining), 0))

    async def _discover_urls(self) -> list[str]:
        urls, _source = await self._discover_url_candidates()
        return urls

    async def _discover_url_candidates(self) -> tuple[list[str], str]:
        mode = self.settings.flaresolverr_discovery_mode
        if mode == "docker":
            if self.settings.autostart_flaresolverr:
                self._ensure_docker_containers()
            urls = self._discover_docker_urls()
            if urls:
                return urls, "docker"
            return (
                build_local_flaresolverr_urls(
                    host=self.settings.flaresolverr_host,
                    base_port=self.settings.flaresolverr_base_port,
                    count=self.settings.flaresolverr_count,
                ),
                "docker_fallback",
            )

        if mode == "ports":
            return (
                build_local_flaresolverr_urls(
                    host=self.settings.flaresolverr_host,
                    base_port=self.settings.flaresolverr_base_port,
                    count=self.settings.flaresolverr_count,
                ),
                "ports",
            )

        env_urls = split_csv_urls(os.environ.get("FLARESOLVERR_URLS"))
        if env_urls:
            return env_urls, "env"

        generated = build_local_flaresolverr_urls(
            host=self.settings.flaresolverr_host,
            base_port=self.settings.flaresolverr_base_port,
            count=self.settings.flaresolverr_count,
        )
        if generated:
            return generated, "generated_local"
        return list(LEGACY_DEFAULT_FLARESOLVERR_URLS), "legacy_fallback"

    def _expected_healthy_count(self, candidate_urls: list[str], source: str) -> int:
        if not candidate_urls:
            return 1

        if source == "env":
            return len(candidate_urls)

        if source in {"docker", "docker_fallback", "ports"}:
            configured_count = self.settings.flaresolverr_count
            if configured_count > 0:
                return min(configured_count, len(candidate_urls))
            return len(candidate_urls)

        return 1

    async def _is_healthy(self, url: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=20)
        session_id = ""
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json={"cmd": "sessions.create"}) as response:
                    if response.status >= 400:
                        logger.warning("FlareSolverr health check failed for %s", url)
                        return False
                    payload = await response.json(content_type=None)
                    session_id = payload.get("session", "") or ""
                if session_id:
                    await http.post(
                        url,
                        json={"cmd": "sessions.destroy", "session": session_id},
                    )
            return True
        except Exception as exc:
            logger.warning("FlareSolverr unhealthy at %s: %s", url, exc)
            return False

    def _discover_docker_urls(self) -> list[str]:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label={self.settings.flaresolverr_docker_label}",
                "--format",
                "{{.Names}}|{{.Ports}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("docker ps failed: %s", result.stderr.strip())
            return []

        urls: list[str] = []
        for line in result.stdout.splitlines():
            for port in self._extract_host_ports(line):
                urls.append(f"http://{self.settings.flaresolverr_host}:{port}/v1")
        return self._dedupe(urls)

    def _extract_host_ports(self, ports_text: str) -> list[str]:
        return re.findall(r":(\d+)->8191/tcp", ports_text)

    def _ensure_docker_containers(self) -> None:
        label_key, label_value = self._parse_label(self.settings.flaresolverr_docker_label)
        for index in range(self.settings.flaresolverr_count):
            name = f"{self.settings.flaresolverr_container_prefix}-{index + 1}"
            host_port = self.settings.flaresolverr_base_port + index

            inspect = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^{name}$",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect.returncode == 0 and inspect.stdout.strip():
                logger.info("Starting existing FlareSolverr container %s", name)
                subprocess.run(["docker", "start", name], check=False, capture_output=True)
                continue

            logger.info(
                "Creating FlareSolverr container %s on host port %d",
                name,
                host_port,
            )
            cmd = [
                "docker",
                "run",
                "-d",
                "--restart",
                "unless-stopped",
                "--name",
                name,
                "--label",
                f"{label_key}={label_value}",
                "-p",
                f"{host_port}:8191",
                self.settings.flaresolverr_docker_image,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                logger.warning(
                    "Failed to start FlareSolverr container %s: %s",
                    name,
                    result.stderr.strip(),
                )

    def _parse_label(self, raw_label: str) -> tuple[str, str]:
        if "=" not in raw_label:
            return raw_label, "true"
        key, value = raw_label.split("=", 1)
        return key.strip(), value.strip()

    def _dedupe(self, urls: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered
