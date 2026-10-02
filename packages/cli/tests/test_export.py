"""Round-trip canonical and dig exports through the discovered CLI."""

from pathlib import Path

import duckdb
import pyarrow.parquet as parquet
import pytest
from sherd_cli.commands.demo import build
from sherd_cli.main import create_app
from sherd_core import Store
from sherd_core.models import FACT_TABLES
from sherd_insights import DigParams, registry
from typer.testing import CliRunner


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "UTC")
    path = tmp_path / "demo.duckdb"
    build(path)
    return path


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_all_tables_round_trip_read_only(demo: Path, tmp_path: Path, fmt: str) -> None:
    before = demo.read_bytes()
    out = tmp_path / "export files"
    result = CliRunner().invoke(
        create_app(), ["export", "--all", "--demo", "--format", fmt, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    with Store.open(demo, read_only=True) as store, duckdb.connect() as conn:
        for table, count in store.table_counts().items():
            target = out / f"{table}.{fmt}"
            reader = "read_parquet" if fmt == "parquet" else "read_csv_auto"
            assert conn.execute(f"SELECT count(*) FROM {reader}(?)", [str(target)]).fetchone() == (
                count,
            )
            assert f"{target}: {count} rows" in result.stdout
            if fmt == "parquet" and count:
                assert parquet.ParquetFile(target).metadata.row_group(0).column(0).compression == (
                    "ZSTD"
                )
    assert demo.read_bytes() == before
    assert len(list(out.iterdir())) == len(FACT_TABLES)
    assert "Open: duckdb -c" in result.stdout
    assert "Python/Jupyter: import duckdb; duckdb.read_" in result.stdout


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_dig_round_trip(demo: Path, tmp_path: Path, fmt: str) -> None:
    dig_id = "messages.volume_by_contact"
    with Store.open(demo, read_only=True) as store:
        expected = registry.discover()[dig_id].compute(store, DigParams()).data
    out = tmp_path / "digs"
    result = CliRunner().invoke(
        create_app(), ["export", "--dig", dig_id, "--demo", "--format", fmt, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    with duckdb.connect() as conn:
        reader = "read_parquet" if fmt == "parquet" else "read_csv_auto"
        actual = conn.execute(f"SELECT * FROM {reader}(?)", [str(out / f"{dig_id}.{fmt}")])
        assert actual.to_arrow_table().num_rows == expected.num_rows


def test_refuses_overwrite_before_writing_any_files(demo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    target = out / "media_plays.parquet"
    target.write_bytes(b"keep me")
    args = ["export", "--all", "--demo", "--out", str(out)]
    result = CliRunner().invoke(create_app(), args)
    assert result.exit_code == 1
    assert "--force" in result.stderr
    assert list(out.iterdir()) == [target]
    assert target.read_bytes() == b"keep me"
    forced = CliRunner().invoke(create_app(), [*args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert parquet.read_table(target).num_rows > 0


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["messages", "--all"],
        ["--all", "--dig", "missing"],
        ["--dig", "missing"],
        ["--all", "--db", "missing", "--demo"],
    ],
)
def test_invalid_selections_are_one_line(demo: Path, args: list[str]) -> None:
    result = CliRunner().invoke(create_app(), ["export", *args])
    assert result.exit_code == 1
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.output
