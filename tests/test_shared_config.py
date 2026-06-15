from __future__ import annotations

from types import SimpleNamespace

import shared.config as shared_config
from shared.config import RabbitMQSettings


def test_rabbitmq_settings_auto_resolves_current_docker_port(monkeypatch) -> None:
    docker_ps = SimpleNamespace(
        returncode=0,
        stdout=(
            "rabbitmqbroker|rabbitmq:4.3.0-rc.0-management-alpine|"
            "0.0.0.0:32770->5672/tcp, [::]:32770->5672/tcp\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(shared_config.subprocess, "run", lambda *args, **kwargs: docker_ps)

    settings = RabbitMQSettings.from_env({"RABBITMQ_URL": "auto"})

    assert settings.url == "amqp://guest:guest@127.0.0.1:32770/%2F"


def test_rabbitmq_settings_auto_prefers_named_container_hint(monkeypatch) -> None:
    docker_ps = SimpleNamespace(
        returncode=0,
        stdout=(
            "other-service|custom/app|0.0.0.0:32000->5672/tcp\n"
            "rabbitmqbroker|rabbitmq:4.3.0-management|0.0.0.0:32770->5672/tcp\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(shared_config.subprocess, "run", lambda *args, **kwargs: docker_ps)

    settings = RabbitMQSettings.from_env(
        {
            "RABBITMQ_URL": "auto",
            "RABBITMQ_DOCKER_CONTAINER_NAME": "rabbitmqbroker",
        }
    )

    assert settings.url == "amqp://guest:guest@127.0.0.1:32770/%2F"


def test_rabbitmq_settings_auto_falls_back_to_default_port_when_docker_fails(
    monkeypatch,
) -> None:
    docker_ps = SimpleNamespace(returncode=1, stdout="", stderr="access denied")
    monkeypatch.setattr(shared_config.subprocess, "run", lambda *args, **kwargs: docker_ps)

    settings = RabbitMQSettings.from_env({"RABBITMQ_URL": "auto"})

    assert settings.url == "amqp://guest:guest@127.0.0.1:5672/%2F"
