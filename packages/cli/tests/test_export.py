"""Round-trip canonical and dig exports through the discovered CLI."""

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.csv as arrow_csv
import pyarrow.parquet as parquet
import pytest
from sherd_cli.commands.demo import build
from sherd_cli.commands.export import Format, write_data
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
        conn.execute("SET TimeZone = 'UTC'")
        for table, count in store.table_counts().items():
            target = out / f"{table}.{fmt}"
            reader = "read_parquet" if fmt == "parquet" else "read_csv_auto"
            assert conn.execute(f"SELECT count(*) FROM {reader}(?)", [str(target)]).fetchone() == (
                count,
            )
            columns = store.query(f'SELECT * FROM "{table}" LIMIT 0').column_names
            projection = ", ".join(
                f'CAST("{column}" AS VARCHAR) AS "{column}"' for column in columns
            )
            expected = store.query(f'SELECT {projection} FROM "{table}" ORDER BY id').to_pylist()
            options = ", all_varchar=true" if fmt == "csv" else ""
            actual = (
                conn.execute(
                    f"SELECT {projection} FROM {reader}(?{options}) ORDER BY id",
                    [str(target)],
                )
                .to_arrow_table()
                .to_pylist()
            )
            assert actual == expected
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
@pytest.mark.parametrize("zone", ["UTC", "America/New_York"])
def test_dig_round_trip(
    demo: Path, tmp_path: Path, fmt: str, zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", zone)
    dig_id = "messages.volume_by_contact"
    with Store.open(demo, read_only=True) as store:
        expected = registry.discover()[dig_id].compute(store, DigParams(tz=zone)).data
    out = tmp_path / "digs"
    result = CliRunner().invoke(
        create_app(), ["export", "--dig", dig_id, "--demo", "--format", fmt, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    with duckdb.connect() as conn:
        reader = "read_parquet" if fmt == "parquet" else "read_csv_auto"
        actual = conn.execute(f"SELECT * FROM {reader}(?)", [str(out / f"{dig_id}.{fmt}")])
        assert actual.to_arrow_table().to_pylist() == expected.to_pylist()


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


def test_output_file_is_friendly(demo: Path, tmp_path: Path) -> None:
    out = tmp_path / "file"
    out.write_text("keep")
    result = CliRunner().invoke(create_app(), ["export", "--all", "--demo", "--out", str(out)])
    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "Output path is a file. Choose a directory with --out DIR."
    ]
    assert out.read_text() == "keep"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", ["write", "rename"])
def test_export_failure_leaves_no_partial_set(
    demo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool, failure: str
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    if force:
        for name in FACT_TABLES:
            (out / f"{name}.parquet").write_bytes(name.encode())
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    original_export = Store.export
    original_replace = Path.replace
    calls = 0

    def export(self: Store, sql: str, params: object, path: Path, fmt: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"partial")
            raise OSError("synthetic disk failure")
        return original_export(self, sql, [], path, "parquet")

    def replace(self: Path, target: Path) -> Path:
        nonlocal calls
        if self.suffix == ".parquet":
            calls += 1
            if calls == 2:
                raise PermissionError("synthetic permission failure")
        return original_replace(self, target)

    if failure == "write":
        monkeypatch.setattr(Store, "export", export)
    else:
        monkeypatch.setattr(Path, "replace", replace)
    args = ["export", "--all", "--demo", "--out", str(out)]
    if force:
        args.append("--force")
    result = CliRunner().invoke(create_app(), args)
    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "Cannot write exports. Choose a writable directory with --out DIR."
    ]
    assert "rows" not in result.stdout
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@pytest.mark.parametrize("fmt", [Format.csv, Format.parquet])
def test_nested_and_quoted_dig_values_round_trip(tmp_path: Path, fmt: Format) -> None:
    data = pa.table(
        {"text": ['synthetic, "quote"\nnext line', None], "items": [["one", "two"], []]}
    )
    target = tmp_path / f"nested.{fmt}"
    write_data(data, target, fmt)
    actual = (
        parquet.read_table(target)
        if fmt == Format.parquet
        else arrow_csv.read_csv(
            target, convert_options=arrow_csv.ConvertOptions(strings_can_be_null=True)
        )
    )
    expected = data.to_pylist()
    if fmt == Format.csv:
        for row in expected:
            row["items"] = str(row["items"])
    assert actual.to_pylist() == expected
