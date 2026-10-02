"""Run the web app on loopback only."""

import webbrowser
from pathlib import Path
from typing import Any

import uvicorn

from sherd_web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def url(port: int) -> str:
    return f"http://{HOST}:{port}/"


def config(db_path: Path, *, demo: bool, port: int = DEFAULT_PORT) -> uvicorn.Config:
    """Server settings: 127.0.0.1 only, no proxy headers, no access log (it holds paths)."""
    return uvicorn.Config(
        create_app(db_path, demo=demo),
        host=HOST,
        port=port,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level="warning",
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
