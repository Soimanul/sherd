"""Export canonical tables and deterministic insight data."""

import re
import shlex
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import duckdb
import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as parquet
import typer
from sherd_core import Store
from sherd_core.models import FACT_TABLES
from sherd_core.paths import default_db_path
from sherd_core.store import StoreError
from sherd_insights import DigParams, registry

from sherd_cli.commands.demo import demo_db_path
from sherd_cli.commands.dig import local_zone


class Format(StrEnum):
    csv = "csv"
    parquet = "parquet"


def write_data(data: pa.Table, path: Path, fmt: Format) -> None:
    if fmt == Format.parquet:
        parquet.write_table(data, path, compression="zstd")
    else:
        # Arrow CSV does not support nested canonical values; digs may also return lists.
        for index, field in enumerate(data.schema):
            if pa.types.is_nested(field.type):
                data = data.set_column(
                    index, field.name, pa.array([str(v) for v in data[field.name].to_pylist()])
                )
        csv.write_csv(data, path)


def register(app: typer.Typer) -> None:
    @app.command(name="export")
    def export_command(
        tables: Annotated[list[str] | None, typer.Argument(help="Canonical table names")] = None,
        all_tables: Annotated[
            bool, typer.Option("--all", help="Export all canonical tables")
        ] = False,
        fmt: Annotated[Format, typer.Option("--format", help="Output format")] = Format.parquet,
        out: Annotated[Path, typer.Option("--out", help="Output directory")] = Path("exports"),
        dig: Annotated[str | None, typer.Option(help="Export an insight's data instead")] = None,
        db: Annotated[Path | None, typer.Option(help="Database path")] = None,
        demo: Annotated[bool, typer.Option(help="Use the demo database")] = False,
        force: Annotated[bool, typer.Option(help="Replace existing export files")] = False,
    ) -> None:
        """Export tables or an insight as CSV or Parquet.

        Example: sherd export --all --format parquet --out exports --demo
        """
        try:
            if db is not None and demo:
                raise ValueError("Choose --db or --demo.")
            if (dig and (tables or all_tables)) or (tables and all_tables):
                raise ValueError("Choose TABLE names, --all, or --dig ID.")
            names = list(FACT_TABLES) if all_tables else list(dict.fromkeys(tables or []))
            if dig is None and not names:
                raise ValueError("Choose TABLE names, --all, or --dig ID.")
            unknown = set(names) - set(FACT_TABLES)
            if unknown:
                raise ValueError(
                    f"Unknown table {sorted(unknown)[0]!r}; choose {', '.join(FACT_TABLES)}."
                )
            insight = None
            if dig is not None:
                insight = registry.discover().get(dig)
                if insight is None:
                    raise ValueError(f"Unknown insight {dig!r}; run sherd show --list.")
                if not re.fullmatch(r"[\w.-]+", dig) or dig in (".", ".."):
                    raise ValueError("Unsafe insight id; choose an id from sherd show --list.")
                names = [dig]
            path = db or (demo_db_path() if demo else default_db_path())
            if not path.is_file():
                raise ValueError("No database yet. Run `sherd demo` or `sherd dig PATH`.")
            targets = [out / f"{name}.{fmt.value}" for name in names]
            for target in targets:
                if target.exists() and not force:
                    raise ValueError(f"File exists: {target}; use --force to replace it.")
            with Store.open(path, read_only=True) as store:
                data = insight.compute(store, DigParams(tz=local_zone())).data if insight else None
                out.mkdir(parents=True, exist_ok=True)
                for name, target in zip(names, targets, strict=True):
                    if data is not None:
                        write_data(data, target, fmt)
                        count = data.num_rows
                    else:
                        count = store.export(f'SELECT * FROM "{name}"', [], target, fmt.value)
                    typer.echo(f"{target}: {count} rows")
            first = str(targets[0])
            reader = "read_parquet" if fmt == Format.parquet else "read_csv_auto"
            sql_path = first.replace("'", "''")
            open_sql = f"SELECT * FROM {reader}('{sql_path}');"
            typer.echo(f"Open: duckdb -c {shlex.quote(open_sql)}")
            python_reader = "read_parquet" if fmt == Format.parquet else "read_csv"
            typer.echo(f"Python/Jupyter: import duckdb; duckdb.{python_reader}({first!r}).df()")
        except (OSError, ValueError, StoreError, duckdb.Error, pa.ArrowException) as error:
            typer.echo(" ".join(str(error).splitlines()), err=True)
            raise typer.Exit(1) from None
