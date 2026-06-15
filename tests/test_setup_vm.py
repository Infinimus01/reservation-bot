from __future__ import annotations

from argparse import Namespace

from setup_vm import build_plan, write_bundle


def build_args(
    tmp_path,
    *,
    master: bool,
    worker: bool,
    concurrency: int,
    master_host: str = "",
    rabbitmq_host: str = "",
    rabbitmq_password: str = "change-me",
) -> Namespace:
    return Namespace(
        master=master,
        worker=worker,
        concurrency=concurrency,
        project_dir=str(tmp_path / "project"),
        output_dir="generated",
        vm_name="vm-test",
        service_user="selenium",
        master_host=master_host,
        master_port=8000,
        rabbitmq_host=rabbitmq_host,
        rabbitmq_port=5672,
        rabbitmq_management_port=15672,
        rabbitmq_user="selenium",
        rabbitmq_password=rabbitmq_password,
        master_api_key="api-key",
        flaresolverr_base_port=8191,
    )


def test_master_bundle_uses_requested_concurrency(tmp_path) -> None:
    plan = build_plan(
        build_args(
            tmp_path,
            master=True,
            worker=False,
            concurrency=10,
            master_host="10.0.0.5",
            rabbitmq_password="secret value",
        )
    )

    write_bundle(plan)

    env_text = plan.env_path.read_text(encoding="utf-8")
    assert "MASTER_URL=http://10.0.0.5:8000" in env_text
    assert "RABBITMQ_URL=amqp://selenium:secret%20value@127.0.0.1:5672/%2F" in env_text
    assert "WORKER_MAX_TASKS=10" in env_text
    assert "RABBITMQ_WORKER_PREFETCH_COUNT=10" in env_text
    assert "WORKER_FLARESOLVERR_COUNT=10" in env_text
    assert "FLARESOLVERR_INSTANCE_COUNT=10" in env_text

    assert (plan.systemd_dir / "selenium-rabbitmq.service").exists()
    assert (plan.systemd_dir / "selenium-master.service").exists()
    assert (plan.systemd_dir / "selenium-reporting.service").exists()
    assert (plan.systemd_dir / "selenium-availability.service").exists()
    assert (plan.systemd_dir / "selenium-worker.service").exists()
    assert (plan.output_dir / "docker-compose.rabbitmq.yml").exists()
    assert (plan.output_dir / "rabbitmq.env").exists()


def test_worker_bundle_reuses_master_host_for_rabbitmq_by_default(tmp_path) -> None:
    plan = build_plan(
        build_args(
            tmp_path,
            master=False,
            worker=True,
            concurrency=6,
            master_host="10.0.0.8",
        )
    )

    write_bundle(plan)

    env_text = plan.env_path.read_text(encoding="utf-8")
    assert "MASTER_URL=http://10.0.0.8:8000" in env_text
    assert "RABBITMQ_URL=amqp://selenium:change-me@10.0.0.8:5672/%2F" in env_text
    assert "GOOGLE_SHEETS_ENABLED=false" in env_text
    assert "WORKER_MAX_TASKS=6" in env_text
    assert "WORKER_FLARESOLVERR_COUNT=6" in env_text

    assert (plan.systemd_dir / "selenium-worker.service").exists()
    assert not (plan.systemd_dir / "selenium-master.service").exists()
    assert not (plan.output_dir / "docker-compose.rabbitmq.yml").exists()


def test_worker_bundle_uses_placeholders_when_hosts_are_missing(tmp_path) -> None:
    plan = build_plan(
        build_args(
            tmp_path,
            master=False,
            worker=True,
            concurrency=4,
        )
    )

    write_bundle(plan)

    env_text = plan.env_path.read_text(encoding="utf-8")
    readme_text = (plan.output_dir / "README.md").read_text(encoding="utf-8")
    assert "MASTER_URL=http://master.internal:8000" in env_text
    assert "RABBITMQ_URL=amqp://selenium:change-me@rabbitmq.internal:5672/%2F" in env_text
    assert "placeholder values" in readme_text
