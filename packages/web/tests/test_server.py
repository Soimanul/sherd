from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sherd_web import server
from sherd_web.app import create_app
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
