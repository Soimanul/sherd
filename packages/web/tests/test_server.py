import asyncio
import logging
import re
import socket
from pathlib import Path
from unittest.mock import Mock

import httpx2 as httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.telemetry import _runtime
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry._logs import _internal as logs_internal
from opentelemetry.metrics import _internal as metrics_internal
from sherd_web import server
from sherd_web.app import CSP, create_app
from sherd_web.deps import Settings


def settings(config: uvicorn.Config) -> Settings:
    assert isinstance(config.app, FastAPI)
    value: Settings = config.app.state.settings
    return value


def test_config_binds_loopback_only(tmp_path: Path) -> None:
    config = server.config(tmp_path / "life.duckdb", demo=False, port=9123)

    assert config.host == "127.0.0.1"
    assert config.port == 9123
    assert config.proxy_headers is False
    assert config.access_log is False
    assert server.url(9123) == "http://127.0.0.1:9123/"


def test_config_serves_the_requested_database(tmp_path: Path) -> None:
    config = server.config(tmp_path / "demo.duckdb", demo=True)

    assert config.port == server.DEFAULT_PORT == 8765
    assert settings(config).db_path == tmp_path / "demo.duckdb"
    assert settings(config).demo is True
    assert not (tmp_path / "demo.duckdb").exists()


def test_create_app_has_no_api_docs(tmp_path: Path) -> None:
    # The docs pages load Swagger/ReDoc from a CDN, which the CSP and the privacy rules forbid.
    app = create_app(tmp_path / "x.duckdb", demo=False)

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_requests_emit_no_telemetry_with_inherited_global_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Capture providers use API-only no-op spans; mocks record every emitted
    # span, metric measurement and log without requiring an SDK/exporter extra.
    tracer = Mock(wraps=trace.NoOpTracer())
    meter = Mock()
    telemetry_logger = Mock()
    tracer_provider = Mock()
    tracer_provider.get_tracer.return_value = tracer
    meter_provider = Mock()
    meter_provider.get_meter.return_value = meter
    logger_provider = Mock()
    logger_provider.get_logger.return_value = telemetry_logger
    # Replace the globals directly so convenience helpers and accessor calls
    # both see the capture providers, and monkeypatch restores them afterwards.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", tracer_provider)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", meter_provider)
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER", logger_provider)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

    def explode() -> None:
        raise RuntimeError("private-message-probe")

    # Positive control proves these capture providers observe all three signals.
    control = FastAPI(telemetry={"auto_configure": False})
    control.add_api_route("/private-location-probe", explode)
    with TestClient(control, raise_server_exceptions=False) as client:
        assert (
            client.get("/private-location-probe?contact=private-contact-probe").status_code == 500
        )
    assert tracer.start_as_current_span.called
    assert tracer.start_span.called
    assert meter.create_histogram.return_value.record.called
    assert meter.create_up_down_counter.return_value.add.called
    assert telemetry_logger.emit.called
    for capture in (tracer, meter, telemetry_logger):
        capture.reset_mock()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")

    def no_exporter_setup(signal: str) -> None:
        pytest.fail("FastAPI consulted the environment for an exporter")

    monkeypatch.setattr(_runtime, "_export_endpoint", no_exporter_setup)
    app = create_app(tmp_path / "missing.duckdb", demo=False)
    app.add_api_route("/private-location-probe", explode)
    app.add_api_route("/private-success-probe", lambda: PlainTextResponse("ok"))
    configured = list(_runtime._configured)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        assert (
            client.get("/private-location-probe?contact=private-contact-probe").status_code == 500
        )
        assert client.get("/private-success-probe?contact=private-contact-probe").status_code == 200
        assert client.get("/private-missing-probe?contact=private-contact-probe").status_code == 404
    assert tracer.mock_calls == []
    assert meter.mock_calls == []
    assert telemetry_logger.mock_calls == []
    assert _runtime._configured == configured


@pytest.mark.parametrize("debug", [False, True])
def test_live_server_error_logging_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, debug: bool
) -> None:
    monkeypatch.setenv("SHERD_DEBUG", "1" if debug else "0")
    config = server.config(tmp_path / "missing.duckdb", demo=False, port=0)
    assert isinstance(config.app, FastAPI)

    def explode() -> None:
        raise RuntimeError("private-message-probe")

    config.app.add_api_route("/private-location-probe", explode)
    # Uvicorn normally sends these to the terminal; also capture them in caplog.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)
    caplog.set_level(logging.ERROR)

    async def request() -> httpx.Response:
        ready = asyncio.Event()

        class LiveServer(uvicorn.Server):
            async def startup(self, sockets: list[socket.socket] | None = None) -> None:
                await super().startup(sockets=sockets)
                ready.set()

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            live = LiveServer(config)
            task = asyncio.create_task(live.serve(sockets=[listener]))
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
                async with httpx.AsyncClient() as client:
                    return await client.get(
                        f"http://127.0.0.1:{port}/private-location-probe?contact=private-query-probe"
                    )
            finally:
                live.should_exit = True
                await asyncio.wait_for(task, timeout=10)

    response = asyncio.run(request())
    assert response.status_code == 500
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["cache-control"] == "no-store"
    assert "private-message-probe" not in response.text
    records = [record for record in caplog.records if record.name == "sherd.web"]
    assert len(records) == 1
    match = re.fullmatch(
        r"request failed: error=RuntimeError request_id=([a-f0-9]{32})", records[0].message
    )
    assert match is not None
    assert match[1] in response.text
    if debug:
        assert "private-message-probe" in caplog.text
        assert "Traceback" in caplog.text
        assert any(record.exc_info for record in caplog.records if record.name == "uvicorn.error")
    else:
        assert "private-message-probe" not in caplog.text
        assert "private-location-probe" not in caplog.text
        assert "private-query-probe" not in caplog.text
        assert not any(record.exc_info for record in caplog.records)
