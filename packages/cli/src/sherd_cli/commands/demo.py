"""`sherd demo`: build a demo database from connector fixtures and synthetic data (PLAN §5)."""

import time
from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sherd_connectors import registry, synth
from sherd_connectors.pipeline import run_import
from sherd_connectors.testing import export_path, load_meta, variants
from sherd_core import Store
from sherd_core.paths import sherd_home
from sherd_core.store import StoreError


def demo_db_path() -> Path:
    return sherd_home() / "demo.duckdb"


def build(path: Path) -> tuple[int, int, dict[str, int]]:
    """Import every connector's fixture variants and the synth `demo` profile into `path`.

    Returns (rows seen, rows inserted, rows per table).
    """
    seen = inserted = 0
    with Store.open(path) as store:
        missing_fixtures = False
        for connector in registry.discover().values():
            try:
                source_variants = variants(connector)
            except FileNotFoundError:
                # Source-tree golden fixtures are intentionally absent from installed wheels.
                missing_fixtures = True
                continue
            for variant in source_variants:
                stats = run_import(store, connector, export_path(variant), load_meta(variant))
                seen, inserted = seen + stats.seen, inserted + stats.inserted
        if missing_fixtures:
            typer.echo("Fixture exports are not installed; using synthetic demo data.")
        stats = synth.import_profile(store, "demo")
        seen, inserted = seen + stats.seen, inserted + stats.inserted
        return seen, inserted, store.table_counts()


def register(app: typer.Typer) -> None:
    @app.command()
    def demo(
        db: Annotated[
            Path | None, typer.Option(help="Database to build (default: $SHERD_HOME/demo.duckdb)")
        ] = None,
        force: Annotated[bool, typer.Option(help="Delete the database and rebuild it")] = False,
    ) -> None:
        """Build a demo database from synthetic data, so you can explore without exports.

        Example: sherd demo
        """
        console = Console(soft_wrap=True)
        path = db or demo_db_path()
        start = time.perf_counter()
        try:
            if force:
                for stale in (path, path.with_name(path.name + ".wal")):
                    stale.unlink(missing_ok=True)
            seen, inserted, counts = build(path)
        except (OSError, ValueError, StoreError, registry.RegistryError, duckdb.Error) as error:
            message = " ".join(str(error).splitlines()).rstrip(".; ")
            if not any(step in message.lower() for step in ("run ", "choose ", "check ", "use ")):
                message += ". Run sherd demo --help"
            message += "."
            Console(stderr=True, soft_wrap=True).print(
                f"[red]error:[/] {escape(message)}",
                highlight=False,
            )
            raise typer.Exit(1) from None
        seconds = time.perf_counter() - start

        table = Table(title="Demo database", title_justify="left")
        table.add_column("table")
        table.add_column("rows", justify="right")
        for name, count in counts.items():
            table.add_row(name, f"{count:,}")
        console.print(table)
        console.print(
            f"{inserted:,} new rows ({seen:,} seen) in {seconds:.1f} s → {path}", highlight=False
        )
        if inserted == 0:
            console.print("Already built; use --force to rebuild it.")
