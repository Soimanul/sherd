"""Run the web app on loopback only."""

import logging
import os
import webbrowser
from copy import deepcopy
from pathlib import Path
from typing import Any

import uvicorn

from sherd_web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class PrivateErrors(logging.Filter):
    """Keep ASGI exception details out of terminal logs unless explicitly requested."""

    def filter(self, record: logging.LogRecord) -> bool:
        if os.environ.get("SHERD_DEBUG") != "1":
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def url(port: int) -> str:
    return f"http://{HOST}:{port}/"


def config(db_path: Path, *, demo: bool, port: int = DEFAULT_PORT) -> uvicorn.Config:
    """Server settings: 127.0.0.1 only, no proxy headers, no access log (it holds paths)."""
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["filters"] = {"private_errors": {"()": PrivateErrors}}
    log_config["loggers"]["uvicorn.error"]["filters"] = ["private_errors"]
    return uvicorn.Config(
        create_app(db_path, demo=demo),
        host=HOST,
        port=port,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level="warning",
        log_config=log_config,
    )


class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, open_browser: bool) -> None:
        super().__init__(config)
        self._open_browser = open_browser

    async def startup(self, *args: Any, **kwargs: Any) -> None:
        await super().startup(*args, **kwargs)
        if self.started and self._open_browser:
            webbrowser.open(url(self.config.port))


def serve(config: uvicorn.Config, *, open_browser: bool) -> None:
    """Run until interrupted; open the browser once the socket is listening."""
    _Server(config, open_browser=open_browser).run()
