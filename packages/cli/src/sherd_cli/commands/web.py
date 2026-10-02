"""`sherd web`: the local web UI on 127.0.0.1."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from sherd_core.paths import default_db_path
from sherd_web import server

from sherd_cli.commands.demo import demo_db_path


def register(app: typer.Typer) -> None:
    @app.command()
    def web(
        db: Annotated[
            Path | None, typer.Option(help="Database to show (default: $SHERD_HOME/life.duckdb)")
        ] = None,
        demo: Annotated[
            bool, typer.Option(help="Show the demo database built by `sherd demo`")
        ] = False,
        port: Annotated[int, typer.Option(min=1, max=65535)] = server.DEFAULT_PORT,
        open_browser: Annotated[
            bool, typer.Option("--open/--no-open", help="Open the browser once it is running")
        ] = True,
    ) -> None:
        """Open your dashboards in the browser. Local only; the database is opened read-only."""
        path = db or (demo_db_path() if demo else default_db_path())
        console = Console(soft_wrap=True, highlight=False)
        console.print(f"sherd web: {server.url(port)}", markup=False)
        if path.is_file():
            console.print(f"Reading {path} (read-only). Ctrl+C stops it.", markup=False)
        else:
            console.print(
                f"No database at {path} yet; the page shows how to start. Ctrl+C stops it.",
                markup=False,
            )
        server.serve(server.config(path, demo=demo, port=port), open_browser=open_browser)
