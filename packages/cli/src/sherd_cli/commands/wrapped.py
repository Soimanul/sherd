"""`sherd wrapped`: the year's shareable PNG cards, made on this machine (PLAN §6, WP-18)."""

from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import typer
from rich.console import Console
from sherd_core import Store
from sherd_core.paths import default_db_path
from sherd_core.store import StoreError
from sherd_insights import wrapped as cards
from sherd_insights.wrapped import WrappedError, WrappedOptions

from sherd_cli.commands.demo import demo_db_path
from sherd_cli.commands.dig import local_zone


def register(app: typer.Typer) -> None:
    @app.command()
    def wrapped(
        year: Annotated[
            int | None,
            typer.Argument(help="Calendar year; defaults to the last full year in the data"),
        ] = None,
        out: Annotated[Path, typer.Option(help="Folder for the PNG files")] = Path(),
        names: Annotated[
            bool,
            typer.Option("--names/--initials", help="Contact names instead of initials"),
        ] = cards.SHOW_NAMES,
        amounts: Annotated[
            bool,
            typer.Option("--amounts/--no-amounts", help="Currency amounts on the money cards"),
        ] = cards.SHOW_AMOUNTS,
        tz: Annotated[
            str | None, typer.Option(help="IANA zone; defaults to the system zone")
        ] = None,
        db: Annotated[Path | None, typer.Option(help="Database file")] = None,
        demo: Annotated[
            bool, typer.Option(help="Use the demo database built by `sherd demo`")
        ] = False,
    ) -> None:
        """Make your year's Wrapped cards as PNG files. Nothing leaves this machine.

        Example: sherd wrapped 2025 --demo --out wrapped
        """
        errors = Console(stderr=True)
        try:
            if db is not None and demo:
                raise ValueError("use --db or --demo, not both")
            zone = tz or local_zone()
            ZoneInfo(zone)
            path = db or (demo_db_path() if demo else default_db_path())
            if not path.is_file():
                typer.echo("No database yet. Run `sherd demo` or `sherd dig PATH`.", err=True)
                raise typer.Exit(1)
            options = WrappedOptions(names=names, amounts=amounts, tz=zone)
            with Store.open(path, read_only=True) as store:
                chosen = year if year is not None else cards.default_year(store, zone)
                if chosen is None:
                    raise ValueError("Wrapped needs a full calendar year of data; none yet")
                rendered = cards.render(store, chosen, options)
            if not rendered:
                raise ValueError(f"nothing to wrap for {chosen}: no rows in that year")
            out.mkdir(parents=True, exist_ok=True)
            for number, (_, png) in enumerate(rendered, start=1):
                target = out / f"wrapped-{chosen}-{number}.png"
                target.write_bytes(png)
                typer.echo(str(target))
        except (
            OSError,
            ValueError,
            ZoneInfoNotFoundError,
            StoreError,
            WrappedError,
            duckdb.Error,
        ) as error:
            errors.print(f"error: {error}", markup=False, highlight=False)
            raise typer.Exit(1) from None
