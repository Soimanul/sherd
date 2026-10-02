from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.base import DetectResult, ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_core import Event, Row, Store, UpsertStats, path_hash


class CountingConnector:
    """Emits `n` events, optionally failing after `fail_after` of them."""

    id = "counting"
    version = "3"
    display_name = "Counting"

    def __init__(self, n: int, fail_after: int | None = None) -> None:
        self.n = n
        self.fail_after = fail_after

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(1.0, "test")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        for i in range(self.n):
            if i == self.fail_after:
                raise ValueError("bad line")
            yield Event(
                source_file="history.txt",
                source_row_id=f"e{i}",
                ts=start + timedelta(minutes=i),
                kind="shell.command",
            )

    def fixtures(self) -> list[Path]:
        return []


@pytest.fixture
def export(tmp_path: Path) -> Path:
    path = tmp_path / "export"
    path.mkdir()
    (path / "history.txt").write_text("synthetic\n")
    return path


def context(export: Path) -> ImportContext:
    return ImportContext(export_root(export), ZoneInfo("America/Chicago"), frozenset())


def imports(store: Store) -> list[dict[str, object]]:
    table = store.query(
        "SELECT connector, connector_version, path_hash, tz, status, rows_seen, rows_inserted"
        " FROM imports ORDER BY started_at"
    )
    return table.to_pylist()


def test_run_import_records_the_ledger(tmp_path: Path, export: Path) -> None:
    with Store.open(tmp_path / "db.duckdb") as store:
        stats = run_import(store, CountingConnector(7), export, context(export))
        again = run_import(store, CountingConnector(7), export, context(export))
        assert stats == UpsertStats(seen=7, inserted=7)
        assert again == UpsertStats(seen=7, inserted=0)
        first = imports(store)[0]
        assert first == {
            "connector": "counting",
            "connector_version": "3",
            "path_hash": path_hash(export),
            "tz": "America/Chicago",
            "status": "succeeded",
            "rows_seen": 7,
            "rows_inserted": 7,
        }
        source = store.query("SELECT DISTINCT source FROM events").column("source").to_pylist()
        assert source == ["counting"]


def test_failed_parse_marks_import_failed_and_reraises(tmp_path: Path, export: Path) -> None:
    with Store.open(tmp_path / "db.duckdb") as store:
        with pytest.raises(ValueError, match="bad line"):
            run_import(store, CountingConnector(12_000, fail_after=11_000), export, context(export))
        [ledger] = imports(store)
        assert ledger["status"] == "failed"
        # Whole batches written before the failure stay, and the ledger counts them exactly.
        assert ledger["rows_inserted"] == store.table_counts()["events"] == 10_000
        stats = run_import(store, CountingConnector(12_000), export, context(export))
        assert stats == UpsertStats(seen=12_000, inserted=2_000)


def test_export_root_of_a_file_is_its_directory(export: Path) -> None:
    assert export_root(export) == export
    assert export_root(export / "history.txt") == export
