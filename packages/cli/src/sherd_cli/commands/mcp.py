"""`sherd mcp`: expose personal data read-only over stdio."""

from pathlib import Path
from typing import Annotated

import duckdb
import typer
from sherd_core.paths import default_db_path
from sherd_core.store import StoreError
from sherd_mcp.server import serve

from sherd_cli.commands.demo import demo_db_path


def register(app: typer.Typer) -> None:
    @app.command()
    def mcp(
        db: Annotated[Path | None, typer.Option(help="Database to query")] = None,
        demo: Annotated[bool, typer.Option(help="Use the database built by `sherd demo`")] = False,
    ) -> None:
        """Run the read-only MCP server over stdio for AI clients."""
        path = db or (demo_db_path() if demo else default_db_path())
        if not path.is_file():
            typer.echo("No database yet. Run `sherd demo` or `sherd dig PATH`.", err=True)
            raise typer.Exit(1)
        try:
            serve(path)
        except (OSError, StoreError, duckdb.Error):
            typer.echo("Could not open the database for read-only MCP access.", err=True)
            raise typer.Exit(1) from None
