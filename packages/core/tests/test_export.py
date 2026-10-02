"""File exports keep values bound and the source database read-only."""

from pathlib import Path
from typing import Literal

import duckdb
import pytest
from sherd_core import Store
from sherd_core.store import QueryNotAllowedError


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_export_read_only_params_and_quoted_path(
    tmp_path: Path, fmt: Literal["csv", "parquet"]
) -> None:
    path = tmp_path / "life.duckdb"
    with Store.open(path):
        pass
    before = path.read_bytes()
    target = tmp_path / f"quoted'file.{fmt}"
    with Store.open(path, read_only=True) as store:
        assert (
            store.export("SELECT ? AS value;", ['synthetic, "quoted"\nnext line'], target, fmt) == 1
        )
    assert path.read_bytes() == before
    with duckdb.connect() as conn:
        reader = "read_csv_auto" if fmt == "csv" else "read_parquet"
        assert conn.execute(f"SELECT value FROM {reader}(?)", [str(target)]).fetchone() == (
            'synthetic, "quoted"\nnext line',
        )


@pytest.mark.parametrize("sql", ["DELETE FROM messages", "SELECT 1; SELECT 2", ""])
def test_export_rejects_non_reads(tmp_path: Path, sql: str) -> None:
    with Store.open(tmp_path / "life.duckdb") as store, pytest.raises(QueryNotAllowedError):
        store.export(sql, [], tmp_path / "bad.csv", "csv")
    assert not (tmp_path / "bad.csv").exists()


def test_sandbox_refuses_export(tmp_path: Path) -> None:
    path = tmp_path / "life.duckdb"
    with Store.open(path):
        pass
    with Store.open(path, read_only=True, sandboxed=True) as store, pytest.raises(duckdb.Error):
        store.export("SELECT 1", [], tmp_path / "bad.csv", "csv")
    assert not (tmp_path / "bad.csv").exists()
