"""Launch the local terminal dashboard."""

from pathlib import Path
from typing import Annotated

import duckdb
import typer
from sherd_core import Store
from sherd_core.paths import default_db_path
from sherd_core.store import StoreError
from sherd_insights import DigParams

from sherd_cli.commands.demo import demo_db_path
from sherd_cli.commands.dig import local_zone
from sherd_cli.tui import Dashboard


def register(app: typer.Typer) -> None:
    @app.command()
    def tui(
        db: Annotated[Path | None, typer.Option(help="Database path")] = None,
        demo: Annotated[bool, typer.Option(help="Use the demo database")] = False,
    ) -> None:
        """Explore your data in a keyboard-driven terminal dashboard.

        Example: sherd tui --demo
        """
        try:
            if db is not None and demo:
                raise ValueError("Choose --db or --demo.")
            path = db or (demo_db_path() if demo else default_db_path())
            if not path.is_file():
                raise ValueError("No database yet. Run `sherd demo` or `sherd dig PATH`.")
            with Store.open(path, read_only=True) as store:
                Dashboard(store, DigParams(tz=local_zone())).run()
        except (OSError, ValueError, StoreError, duckdb.Error) as error:
            typer.echo(" ".join(str(error).splitlines()), err=True)
            raise typer.Exit(1) from None
