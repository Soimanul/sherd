"""List and compute installed insights."""

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from sherd_core import Store
from sherd_core.paths import default_db_path, sherd_home
from sherd_core.store import StoreError
from sherd_insights import DigParams, DigResult, registry
from sherd_insights.charts import json_value, records

from sherd_cli.commands.dig import local_zone


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _json(result: DigResult) -> dict[str, object]:
    return {
        "data": records(result.data),
        "chart": result.chart,
        "narrative": result.narrative,
        "headline": asdict(result.headline) if result.headline else None,
        "text_summary": result.text_summary,
    }


def register(app: typer.Typer) -> None:
    @app.command()
    def show(
        dig_id: Annotated[str | None, typer.Argument(help="Insight id")] = None,
        date_from: Annotated[str | None, typer.Option("--from", help="First local date")] = None,
        date_to: Annotated[
            str | None, typer.Option("--to", help="Last local date (inclusive)")
        ] = None,
        top: Annotated[int, typer.Option(min=1)] = 10,
        granularity: Annotated[str, typer.Option()] = "month",
        tz: Annotated[
            str | None, typer.Option(help="IANA zone; defaults to the system zone")
        ] = None,
        json_output: Annotated[bool, typer.Option("--json")] = False,
        db: Annotated[Path | None, typer.Option()] = None,
        demo: Annotated[bool, typer.Option()] = False,
        list_only: Annotated[bool, typer.Option("--list")] = False,
        markdown: Annotated[bool, typer.Option("--markdown")] = False,
    ) -> None:
        """Explore deterministic insights from your imported messages.

        Example: sherd show --demo
        """
        console = Console(soft_wrap=True)
        try:
            digs = registry.discover()
            if list_only:
                if markdown:
                    typer.echo("| id | title | requires |\n| --- | --- | --- |")
                    for dig in digs.values():
                        typer.echo(f"| {dig.id} | {dig.title} | {', '.join(dig.requires)} |")
                else:
                    for dig in digs.values():
                        console.print(f"{dig.id}: {dig.title}", markup=False, highlight=False)
                return
            if granularity not in ("day", "week", "month", "year"):
                raise ValueError("--granularity must be day, week, month or year")
            zone = tz or local_zone()
            ZoneInfo(zone)
            params = DigParams(
                _date(date_from),
                _date(date_to),
                top,
                cast(Literal["day", "week", "month", "year"], granularity),
                zone,
            )
            if params.date_from and params.date_to and params.date_from > params.date_to:
                raise ValueError("--from must be on or before --to")
            path = db or (sherd_home() / "demo.duckdb" if demo else default_db_path())
            if not path.is_file():
                typer.echo("No database yet. Run `sherd demo` or `sherd dig PATH`.", err=True)
                raise typer.Exit(1)
            with Store.open(path, read_only=True) as store:
                if dig_id is None:
                    for dig in registry.available(store):
                        computed = dig.compute(store, params)
                        if not computed.data.num_rows:
                            continue
                        h = computed.headline
                        label = f"{h.label}: {h.value} {h.unit or ''}" if h else computed.narrative
                        console.print(f"{dig.id}: {label}", markup=False, highlight=False)
                    return
                if dig_id not in digs:
                    raise ValueError(f"unknown insight {dig_id!r}; run sherd show --list")
                computed = digs[dig_id].compute(store, params)
            if json_output:
                typer.echo(json.dumps(json_value(_json(computed)), ensure_ascii=False))
                return
            if computed.headline:
                h = computed.headline
                console.print(f"{h.label}: {h.value} {h.unit or ''}", markup=False)
            console.print(computed.narrative, markup=False, highlight=False)
            table = Table()
            for name in computed.data.column_names:
                table.add_column(name)
            for row in records(computed.data):
                table.add_row(*(Text(str(row[name])) for name in computed.data.column_names))
            console.print(table)
        except (OSError, ValueError, ZoneInfoNotFoundError, StoreError, duckdb.Error) as error:
            message = " ".join(str(error).splitlines()).rstrip(".; ")
            if not any(step in message.lower() for step in ("run ", "choose ", "check ", "use ")):
                message += ". Run sherd show --help"
            message += "."
            Console(stderr=True, soft_wrap=True).print(
                f"error: {message}",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(1) from None
