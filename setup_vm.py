from __future__ import annotations

import argparse
import re
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
DEFAULT_MASTER_HOST = "master.internal"
DEFAULT_RABBITMQ_HOST = "rabbitmq.internal"
DEFAULT_API_KEY = "change-me-api-key"
DEFAULT_RABBITMQ_USER = "selenium"
DEFAULT_RABBITMQ_PASSWORD = "change-me"
DEFAULT_SERVICE_USER = "selenium"
DEFAULT_PROJECT_DIR = Path("/opt/selenium_bot")
DEFAULT_OUTPUT_BASE = Path("deployment_artifacts")
ENV_LINE_RE = re.compile(r"^\s*#?\s*([A-Z0-9_]+)=(.*)$")


@dataclass(frozen=True)
class SetupPlan:
    role: str
    concurrency: int
    project_dir: Path
    output_dir: Path
    vm_name: str
    service_user: str
    master_host: str
    master_port: int
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_management_port: int
    rabbitmq_user: str
    rabbitmq_password: str
    master_api_key: str
    flaresolverr_base_port: int

    @property
    def worker_service_name(self) -> str:
        if self.role == "master":
            return f"{self.vm_name}-worker"
        return self.vm_name

    @property
    def master_url(self) -> str:
        return f"http://{self.master_host}:{self.master_port}"

    @property
    def rabbitmq_url(self) -> str:
        encoded_password = urllib.parse.quote(self.rabbitmq_password, safe="")
        return (
            "amqp://"
            f"{self.rabbitmq_user}:{encoded_password}@"
            f"{self.rabbitmq_host}:{self.rabbitmq_port}/%2F"
        )

    @property
    def env_path(self) -> Path:
        return self.output_dir / ".env.generated"

    @property
    def scripts_dir(self) -> Path:
        return self.output_dir / "scripts"

    @property
    def systemd_dir(self) -> Path:
        return self.output_dir / "systemd"

    @property
    def has_placeholders(self) -> bool:
        return (
            self.master_host in {DEFAULT_MASTER_HOST}
            or self.rabbitmq_host in {DEFAULT_RABBITMQ_HOST}
            or self.master_api_key == DEFAULT_API_KEY
            or self.rabbitmq_password == DEFAULT_RABBITMQ_PASSWORD
        )


def detect_primary_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except OSError:
        return None
    if not ip or ip.startswith("127."):
        return None
    return ip


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Ubuntu deployment artifacts for a master VM or worker VM "
            "without starting any services."
        )
    )
    role_group = parser.add_mutually_exclusive_group(required=True)
    role_group.add_argument("--master", action="store_true", help="Generate master VM setup.")
    role_group.add_argument("--worker", action="store_true", help="Generate worker VM setup.")
    parser.add_argument(
        "--concurrency",
        type=int,
        required=True,
        help="Worker concurrency for this VM. Also used for FlareSolverr count.",
    )
    parser.add_argument(
        "--project-dir",
        default=str(DEFAULT_PROJECT_DIR),
        help="Target project directory on the VM. Default: /opt/selenium_bot",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=(
            "Where to write the generated bundle. Relative paths are resolved "
            "under --project-dir."
        ),
    )
    parser.add_argument(
        "--vm-name",
        default="",
        help="Logical VM name used for worker IDs and generated file labels.",
    )
    parser.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
        help="Linux user that will run the systemd services.",
    )
    parser.add_argument(
        "--master-host",
        default="",
        help="Host or IP used in MASTER_URL.",
    )
    parser.add_argument(
        "--master-port",
        type=int,
        default=8000,
        help="Master API port. Default: 8000",
    )
    parser.add_argument(
        "--rabbitmq-host",
        default="",
        help="RabbitMQ host for worker connections.",
    )
    parser.add_argument(
        "--rabbitmq-port",
        type=int,
        default=5672,
        help="RabbitMQ port. Default: 5672",
    )
    parser.add_argument(
        "--rabbitmq-management-port",
        type=int,
        default=15672,
        help="RabbitMQ management UI port. Default: 15672",
    )
    parser.add_argument(
        "--rabbitmq-user",
        default=DEFAULT_RABBITMQ_USER,
        help="RabbitMQ username. Default: selenium",
    )
    parser.add_argument(
        "--rabbitmq-password",
        default=DEFAULT_RABBITMQ_PASSWORD,
        help="RabbitMQ password. Default: change-me",
    )
    parser.add_argument(
        "--master-api-key",
        default=DEFAULT_API_KEY,
        help="Master API key written into the generated env file.",
    )
    parser.add_argument(
        "--flaresolverr-base-port",
        type=int,
        default=8191,
        help="First local FlareSolverr port. Default: 8191",
    )
    args = parser.parse_args(argv)
    if args.concurrency <= 0:
        parser.error("--concurrency must be greater than 0")
    return args


def resolve_output_dir(project_dir: Path, output_dir: str, role: str) -> Path:
    if output_dir:
        candidate = Path(output_dir)
        if candidate.is_absolute():
            return candidate
        return project_dir / candidate
    return project_dir / DEFAULT_OUTPUT_BASE / role


def build_plan(args: argparse.Namespace) -> SetupPlan:
    role = "master" if args.master else "worker"
    project_dir = Path(args.project_dir).expanduser()
    output_dir = resolve_output_dir(project_dir, args.output_dir, role)

    detected_master_host = args.master_host.strip()
    if not detected_master_host and role == "master":
        detected_master_host = detect_primary_ip() or DEFAULT_MASTER_HOST
    if not detected_master_host:
        detected_master_host = DEFAULT_MASTER_HOST

    rabbitmq_host = args.rabbitmq_host.strip()
    if not rabbitmq_host:
        if role == "master":
            rabbitmq_host = "127.0.0.1"
        elif args.master_host.strip():
            rabbitmq_host = detected_master_host
        else:
            rabbitmq_host = DEFAULT_RABBITMQ_HOST

    vm_name = args.vm_name.strip() or ("master-vm" if role == "master" else "worker-vm")

    return SetupPlan(
        role=role,
        concurrency=args.concurrency,
        project_dir=project_dir,
        output_dir=output_dir,
        vm_name=vm_name,
        service_user=args.service_user.strip() or DEFAULT_SERVICE_USER,
        master_host=detected_master_host,
        master_port=args.master_port,
        rabbitmq_host=rabbitmq_host,
        rabbitmq_port=args.rabbitmq_port,
        rabbitmq_management_port=args.rabbitmq_management_port,
        rabbitmq_user=args.rabbitmq_user.strip() or DEFAULT_RABBITMQ_USER,
        rabbitmq_password=args.rabbitmq_password,
        master_api_key=args.master_api_key.strip() or DEFAULT_API_KEY,
        flaresolverr_base_port=args.flaresolverr_base_port,
    )


def load_env_template() -> str:
    return ENV_EXAMPLE_PATH.read_text(encoding="utf-8")


def apply_env_overrides(template: str, overrides: dict[str, str]) -> str:
    lines = template.splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key not in overrides:
            continue
        lines[index] = f"{key}={overrides[key]}"
        seen.add(key)

    for key, value in overrides.items():
        if key in seen:
            continue
        lines.append(f"{key}={value}")

    return "\n".join(lines) + "\n"


def build_env_overrides(plan: SetupPlan) -> dict[str, str]:
    overrides = {
        "MASTER_URL": plan.master_url,
        "MASTER_API_KEY": plan.master_api_key,
        "RABBITMQ_URL": plan.rabbitmq_url,
        "RABBITMQ_WORKER_PREFETCH_COUNT": str(plan.concurrency),
        "WORKER_ID": plan.worker_service_name,
        "WORKER_NAME": plan.worker_service_name,
        "WORKER_MAX_TASKS": str(plan.concurrency),
        "WORKER_FLARESOLVERR_STARTUP_TIMEOUT_SECONDS": "180",
        "WORKER_FLARESOLVERR_STARTUP_POLL_SECONDS": "3",
        "FLARESOLVERR_DISCOVERY_MODE": "docker",
        "FLARESOLVERR_HOST": "127.0.0.1",
        "FLARESOLVERR_BASE_PORT": str(plan.flaresolverr_base_port),
        "FLARESOLVERR_INSTANCE_COUNT": str(plan.concurrency),
        "WORKER_FLARESOLVERR_COUNT": str(plan.concurrency),
        "WORKER_AUTOSTART_FLARESOLVERR": "true",
    }

    if plan.role == "master":
        overrides.update(
            {
                "MASTER_HOST": "0.0.0.0",
                "MASTER_PORT": str(plan.master_port),
                "MASTER_REQUIRE_AVAILABILITY_TRIGGER": "true",
                "GOOGLE_SHEETS_ENABLED": "true",
            }
        )
    else:
        overrides["GOOGLE_SHEETS_ENABLED"] = "false"

    return overrides


def render_install_prereqs_script(plan: SetupPlan) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="{plan.project_dir}"

sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${{UBUNTU_CODENAME:-$VERSION_CODENAME}}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/screenshots" "$PROJECT_DIR/availability_runs"
cd "$PROJECT_DIR"
uv sync

echo "Prerequisites installed. Review generated files under {plan.output_dir}."
"""


def render_apply_bundle_script(plan: SetupPlan) -> str:
    systemd_unit_names = ["selenium-worker.service"]
    if plan.role == "master":
        systemd_unit_names = [
            "selenium-rabbitmq.service",
            "selenium-master.service",
            "selenium-reporting.service",
            "selenium-availability.service",
            "selenium-worker.service",
        ]
    copy_units = "\n".join(
        f'sudo install -D -m 0644 "$BUNDLE_DIR/systemd/{unit}" "/etc/systemd/system/{unit}"'
        for unit in systemd_unit_names
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="{plan.project_dir}"
BUNDLE_DIR="{plan.output_dir}"

install -D -m 0644 "$BUNDLE_DIR/.env.generated" "$PROJECT_DIR/.env"
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/screenshots" "$PROJECT_DIR/availability_runs"

{copy_units}

sudo systemctl daemon-reload

echo "Generated files installed. Services were not started."
echo "Review $PROJECT_DIR/.env before enabling services."
"""


def render_systemd_service(
    *,
    description: str,
    project_dir: Path,
    service_user: str,
    exec_start: str,
    after: str,
) -> str:
    return f"""[Unit]
Description={description}
After={after}
Wants=network-online.target

[Service]
User={service_user}
WorkingDirectory={project_dir}
EnvironmentFile={project_dir / ".env"}
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def render_rabbitmq_service(plan: SetupPlan) -> str:
    compose_file = plan.output_dir / "docker-compose.rabbitmq.yml"
    return f"""[Unit]
Description=Selenium Bot RabbitMQ
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={plan.output_dir}
ExecStart=/usr/bin/docker compose -f {compose_file} up -d
ExecStop=/usr/bin/docker compose -f {compose_file} down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""


def render_rabbitmq_compose(plan: SetupPlan) -> str:
    return f"""services:
  rabbitmq:
    image: rabbitmq:3-management
    container_name: selenium-rabbitmq
    restart: unless-stopped
    env_file:
      - ./rabbitmq.env
    ports:
      - "{plan.rabbitmq_port}:5672"
      - "127.0.0.1:{plan.rabbitmq_management_port}:15672"
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq

volumes:
  rabbitmq_data:
"""


def render_rabbitmq_env(plan: SetupPlan) -> str:
    return (
        f"RABBITMQ_DEFAULT_USER={plan.rabbitmq_user}\n"
        f"RABBITMQ_DEFAULT_PASS={plan.rabbitmq_password}\n"
    )


def render_readme(plan: SetupPlan) -> str:
    role_summary = {
        "master": (
            "This bundle configures RabbitMQ, master.main, master.reporting_worker, "
            "master.availability_checker, and a local booking worker."
        ),
        "worker": "This bundle configures a booking worker and its local FlareSolverr pool.",
    }[plan.role]
    placeholder_note = (
        "\nThis bundle still contains placeholder values. Review `.env.generated` "
        "before applying it.\n"
        if plan.has_placeholders
        else "\nThis bundle was rendered with concrete host and credential values.\n"
    )
    service_names = ["selenium-worker"]
    if plan.role == "master":
        service_names = [
            "selenium-rabbitmq",
            "selenium-master",
            "selenium-reporting",
            "selenium-availability",
            "selenium-worker",
        ]
    enable_command = " ".join(service_names)
    rabbitmq_note = ""
    if plan.role == "master":
        rabbitmq_note = (
            f"- RabbitMQ will listen on `127.0.0.1:{plan.rabbitmq_management_port}` for "
            "the management UI and on the VM on port "
            f"`{plan.rabbitmq_port}` for AMQP.\n"
        )
    return f"""# {plan.role.title()} VM Setup Bundle

Generated for concurrency `{plan.concurrency}`.

{role_summary}
{placeholder_note}
## Generated Files

- `.env.generated`
- `scripts/install_prereqs.sh`
- `scripts/apply_bundle.sh`
- `systemd/`
{"- `docker-compose.rabbitmq.yml`\n- `rabbitmq.env`\n" if plan.role == "master" else ""}
## Computed Values

- `MASTER_URL={plan.master_url}`
- `RABBITMQ_URL={plan.rabbitmq_url}`
- `WORKER_MAX_TASKS={plan.concurrency}`
- `RABBITMQ_WORKER_PREFETCH_COUNT={plan.concurrency}`
- `WORKER_FLARESOLVERR_COUNT={plan.concurrency}`
{rabbitmq_note}## Apply Order

1. Run `scripts/install_prereqs.sh`
2. Review `./.env.generated`
3. Run `scripts/apply_bundle.sh`
4. Enable services without starting them:

```bash
sudo systemctl enable {enable_command}
```

5. Start them when ready:

```bash
sudo systemctl start {enable_command}
```
"""


def write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def write_bundle(plan: SetupPlan) -> None:
    env_text = apply_env_overrides(load_env_template(), build_env_overrides(plan))
    write_file(plan.env_path, env_text)
    write_file(
        plan.scripts_dir / "install_prereqs.sh",
        render_install_prereqs_script(plan),
        executable=True,
    )
    write_file(
        plan.scripts_dir / "apply_bundle.sh",
        render_apply_bundle_script(plan),
        executable=True,
    )
    write_file(
        plan.systemd_dir / "selenium-worker.service",
        render_systemd_service(
            description="Selenium Bot Booking Worker",
            project_dir=plan.project_dir,
            service_user=plan.service_user,
            exec_start=f"/home/{plan.service_user}/.local/bin/uv run -m worker.worker_main",
            after="network-online.target docker.service",
        ),
    )

    if plan.role == "master":
        write_file(
            plan.systemd_dir / "selenium-master.service",
            render_systemd_service(
                description="Selenium Bot Master API",
                project_dir=plan.project_dir,
                service_user=plan.service_user,
                exec_start=f"/home/{plan.service_user}/.local/bin/uv run -m master.main",
                after="network-online.target docker.service selenium-rabbitmq.service",
            ),
        )
        write_file(
            plan.systemd_dir / "selenium-reporting.service",
            render_systemd_service(
                description="Selenium Bot Reporting Worker",
                project_dir=plan.project_dir,
                service_user=plan.service_user,
                exec_start=(
                    f"/home/{plan.service_user}/.local/bin/uv run -m "
                    "master.reporting_worker"
                ),
                after="network-online.target docker.service selenium-master.service",
            ),
        )
        write_file(
            plan.systemd_dir / "selenium-availability.service",
            render_systemd_service(
                description="Selenium Bot Availability Checker",
                project_dir=plan.project_dir,
                service_user=plan.service_user,
                exec_start=(
                    f"/home/{plan.service_user}/.local/bin/uv run -m "
                    "master.availability_checker"
                ),
                after="network-online.target docker.service selenium-master.service",
            ),
        )
        write_file(plan.output_dir / "docker-compose.rabbitmq.yml", render_rabbitmq_compose(plan))
        write_file(plan.output_dir / "rabbitmq.env", render_rabbitmq_env(plan))
        write_file(
            plan.systemd_dir / "selenium-rabbitmq.service",
            render_rabbitmq_service(plan),
        )

    write_file(plan.output_dir / "README.md", render_readme(plan))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_plan(args)
    write_bundle(plan)
    print(f"Generated {plan.role} VM setup bundle in {plan.output_dir}")
    print(f"Review {plan.env_path} before applying the bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
