import os
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from sherd_cli.main import create_app
from sherd_web import server
from sherd_web.deps import Settings
from typer.testing import CliRunner, Result

Calls = list[tuple[uvicorn.Config, bool]]


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Capture what `sherd web` would serve instead of starting a real server."""
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    calls: Calls = []
    monkeypatch.setattr(
        server, "serve", lambda config, *, open_browser: calls.append((config, open_browser))
    )
    return calls


def settings(config: uvicorn.Config) -> Settings:
    assert isinstance(config.app, FastAPI)
    value: Settings = config.app.state.settings
    return value


def run(*args: str) -> Result:
    return CliRunner().invoke(create_app(), ["web", *args])


def test_no_open_binds_loopback(served: Calls, tmp_path: Path) -> None:
    result = run("--no-open", "--db", str(tmp_path / "life.duckdb"), "--port", "9001")

    assert result.exit_code == 0, result.output
    [(config, open_browser)] = served
    assert config.host == "127.0.0.1"
    assert config.port == 9001
    assert open_browser is False
    assert "http://127.0.0.1:9001/" in result.output
    assert "No database at" in result.output


def test_defaults(served: Calls, tmp_path: Path) -> None:
    result = run()

    assert result.exit_code == 0, result.output
    [(config, open_browser)] = served
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert open_browser is True
    assert settings(config).db_path == tmp_path / "life.duckdb"
    assert settings(config).demo is False


def test_demo_reads_the_demo_database(served: Calls, tmp_path: Path) -> None:
    (tmp_path / "demo.duckdb").touch()

    result = run("--demo", "--no-open")

    assert result.exit_code == 0, result.output
    [(config, _)] = served
    assert settings(config).db_path == tmp_path / "demo.duckdb"
    assert settings(config).demo is True
    assert "read-only" in result.output


def test_rejects_bad_port(served: Calls) -> None:
    result = run("--port", "0")

    assert result.exit_code != 0
    assert served == []


def test_web_disables_inherited_telemetry_before_app_start(
    served: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter_vars = [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
    ]
    for name in exporter_vars:
        monkeypatch.setenv(name, "inherited-probe")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "preserved-probe")
    real_config = server.config

    def checked_config(db_path: Path, *, demo: bool, port: int) -> uvicorn.Config:
        assert os.environ["OTEL_SDK_DISABLED"] == "true"
        assert all(name not in os.environ for name in exporter_vars)
        assert os.environ["OTEL_SERVICE_NAME"] == "preserved-probe"
        return real_config(db_path, demo=demo, port=port)

    monkeypatch.setattr(server, "config", checked_config)
    result = run("--no-open")
    assert result.exit_code == 0, result.output
    assert len(served) == 1
