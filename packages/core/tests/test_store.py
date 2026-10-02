import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from sherd_core import (
    Event,
    Location,
    MediaPlay,
    Message,
    Row,
    Store,
    Transaction,
    UpsertStats,
    row_id,
)
from sherd_core.store import QueryNotAllowedError, ReadOnlyStoreError, StoreError

MessageFactory = Callable[..., Message]
TS = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)


def new_import(store: Store, connector: str = "whatsapp") -> str:
    return store.begin_import(connector, "1.0", "path-hash", "Europe/Bucharest")


def import_ids(store: Store) -> dict[str, str]:
    table = store.query("SELECT source_row_id, import_id FROM messages")
    return dict(zip(*table.to_pydict().values(), strict=True))


# -- idempotency and overlap -----------------------------------------------------------------


def test_reimport_inserts_nothing(store: Store, make_message: MessageFactory) -> None:
    rows = [make_message(i) for i in range(1200)]
    first = new_import(store)
    assert store.upsert(first, "whatsapp", rows, batch_size=500) == UpsertStats(1200, 1200)
    counts = store.table_counts()

    second = new_import(store)
    assert store.upsert(second, "whatsapp", rows, batch_size=500) == UpsertStats(1200, 0)
    assert (
        store.table_counts()
        == counts
        == {
            "messages": 1200,
            "media_plays": 0,
            "transactions": 0,
            "events": 0,
            "locations": 0,
        }
    )
    assert set(import_ids(store).values()) == {first}


def test_overlapping_exports_insert_only_new_rows(
    store: Store, make_message: MessageFactory
) -> None:
    first = new_import(store)
    store.upsert(first, "whatsapp", (make_message(i) for i in range(0, 1000)), batch_size=300)
    second = new_import(store)
    stats = store.upsert(
        second, "whatsapp", (make_message(i) for i in range(500, 1500)), batch_size=300
    )
    assert stats == UpsertStats(seen=1000, inserted=500)
    owners = import_ids(store)
    assert len(owners) == 1500
    assert {owners[f"m{i:06d}"] for i in range(0, 1000)} == {first}
    assert {owners[f"m{i:06d}"] for i in range(1000, 1500)} == {second}


def test_first_write_wins_on_content(store: Store, make_message: MessageFactory) -> None:
    store.upsert(new_import(store), "whatsapp", [make_message(1, text="first")])
    stats = store.upsert(new_import(store), "whatsapp", [make_message(1, text="second")])
    assert stats == UpsertStats(1, 0)
    assert store.query("SELECT text FROM messages").to_pylist() == [{"text": "first"}]


def test_duplicates_within_one_upsert(store: Store, make_message: MessageFactory) -> None:
    rows = [make_message(1, text="a"), make_message(2), make_message(1, text="b")]
    stats = store.upsert(new_import(store), "whatsapp", rows, batch_size=10)
    assert stats == UpsertStats(seen=3, inserted=2)
    assert store.query("SELECT text FROM messages WHERE source_row_id = 'm000001'").to_pylist() == [
        {"text": "a"}
    ]


def test_same_source_row_id_in_different_sources(
    store: Store, make_message: MessageFactory
) -> None:
    import_id = new_import(store)
    store.upsert(import_id, "whatsapp", [make_message(1)])
    assert store.upsert(import_id, "telegram", [make_message(1)]) == UpsertStats(1, 1)


# -- what the store writes -------------------------------------------------------------------


def test_mixed_rows_land_in_their_tables_with_store_fields(store: Store) -> None:
    common: dict[str, Any] = {"source_file": "export/data.json", "ts": TS}
    rows: list[Row] = [
        Message(
            **common,
            source_row_id="m",
            chat_id="c",
            chat_kind="group",
            is_from_me=True,
            kind="media",
            media_type="sticker",
            meta={"edited": True, "at": TS, "n": Decimal("1.5")},
        ),
        MediaPlay(**common, source_row_id="p", media_kind="episode", ms_played=60_000),
        Transaction(
            **common,
            source_row_id="t",
            amount="-9.99",
            currency="EUR",
            account="main",
            balance="100.5",
            kind="card",
        ),
        Event(**common, source_row_id="e", kind="google.search", title="weather"),
        Location(**common, source_row_id="l", lat=44.43, lon=26.1, kind="visit", end_ts=TS),
    ]
    import_id = new_import(store, "synth")
    before = datetime.now(UTC)
    assert store.upsert(import_id, "synth", rows, batch_size=2) == UpsertStats(5, 5)
    assert set(store.table_counts().values()) == {1}

    for row in rows:
        stored = store.query(
            f"SELECT id, source, source_file, source_row_id, import_id, imported_at, meta"
            f" FROM {row.table_name}"
        ).to_pylist()[0]
        assert stored["id"] == row_id("synth", row.source_row_id)
        assert stored["source"] == "synth"
        assert stored["source_file"] == "export/data.json"
        assert stored["import_id"] == import_id
        assert before <= stored["imported_at"] <= datetime.now(UTC)

    message = store.query("SELECT meta, media_type FROM messages").to_pylist()[0]
    assert message == {
        "meta": '{"edited":true,"at":"2024-03-01T09:30:00Z","n":"1.5"}',
        "media_type": "sticker",
    }
    money = store.query("SELECT amount, balance FROM transactions").to_pylist()[0]
    assert money == {"amount": Decimal("-9.99"), "balance": Decimal("100.50")}


def test_aware_non_utc_time_stored_as_same_instant(
    store: Store, make_message: MessageFactory
) -> None:
    local = datetime(2024, 7, 1, 23, 15, tzinfo=timezone(timedelta(hours=-5)))
    store.upsert(new_import(store), "whatsapp", [make_message(1, ts=local)])
    table = store.query("SELECT ts, strftime(ts, '%Y-%m-%d %H:%M') AS shown FROM messages")
    assert str(table.schema.field("ts").type) == "timestamp[us, tz=UTC]"
    stored = table.to_pylist()[0]
    assert stored["ts"] == local
    assert stored["shown"] == "2024-07-02 04:15"


def test_failed_upsert_keeps_written_batches_and_resumes(
    store: Store, make_message: MessageFactory
) -> None:
    def failing() -> Iterator[Message]:
        yield from (make_message(i) for i in range(5))
        raise RuntimeError("parse error")

    first = new_import(store)
    with pytest.raises(RuntimeError, match="parse error"):
        store.upsert(first, "whatsapp", failing(), batch_size=2)
    persisted = store.table_counts()["messages"]
    assert persisted == 4
    assert store.finish_import(first, "failed") == UpsertStats(persisted, persisted)
    second = new_import(store)
    rerun = store.upsert(second, "whatsapp", (make_message(i) for i in range(5)))
    assert rerun == UpsertStats(5, 1)
    assert store.finish_import(second, "succeeded") == rerun
    owners = import_ids(store)
    assert {owners[f"m{i:06d}"] for i in range(4)} == {first}
    assert owners["m000004"] == second


def test_upsert_rejects_non_rows(store: Store) -> None:
    with pytest.raises(TypeError, match="dict"):
        store.upsert(new_import(store), "whatsapp", [{"text": "x"}])  # type: ignore[list-item]  # deliberately wrong type


@pytest.mark.parametrize(("source", "batch_size"), [("", 10), ("whatsapp", 0)])
def test_upsert_argument_checks(store: Store, source: str, batch_size: int) -> None:
    with pytest.raises(ValueError, match=r"source|batch_size"):
        store.upsert(new_import(store), source, [], batch_size=batch_size)


def test_logs_carry_counts_not_content(
    store: Store, make_message: MessageFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="sherd.core")
    import_id = new_import(store)
    store.upsert(import_id, "whatsapp", [make_message(1, text="secret-text-123")])
    store.finish_import(import_id, "succeeded")
    assert import_id in caplog.text
    for content in ("secret-text-123", "Ana", "+15550100", "chat-1", "Chat with Ana"):
        assert content not in caplog.text


# -- imports ---------------------------------------------------------------------------------


def test_import_lifecycle(store: Store, make_message: MessageFactory) -> None:
    import_id = store.begin_import("whatsapp", "1.2", "abc", "America/New_York")
    running = store.query("SELECT * FROM imports WHERE id = ?", [import_id]).to_pylist()[0]
    assert running["status"] == "running"
    assert (running["rows_seen"], running["rows_inserted"], running["finished_at"]) == (0, 0, None)
    assert (running["connector"], running["connector_version"]) == ("whatsapp", "1.2")
    assert (running["path_hash"], running["tz"]) == ("abc", "America/New_York")

    stats = store.upsert(import_id, "whatsapp", [make_message(1), make_message(1)])
    assert store.finish_import(import_id, "succeeded") == stats
    done = store.query("SELECT * FROM imports WHERE id = ?", [import_id]).to_pylist()[0]
    assert (done["status"], done["rows_seen"], done["rows_inserted"]) == ("succeeded", 2, 1)
    assert done["finished_at"] >= done["started_at"]


def test_failed_import_is_recorded(store: Store) -> None:
    import_id = new_import(store)
    store.finish_import(import_id, "failed")
    status = store.query("SELECT status FROM imports WHERE id = ?", [import_id])
    assert status.to_pylist() == [{"status": "failed"}]


def test_finished_or_unknown_imports_cannot_be_used(store: Store) -> None:
    import_id = new_import(store)
    store.finish_import(import_id, "succeeded")
    with pytest.raises(StoreError, match="not running"):
        store.finish_import(import_id, "failed")
    with pytest.raises(StoreError, match="not running"):
        store.upsert(import_id, "whatsapp", [])
    with pytest.raises(StoreError, match="not running"):
        store.upsert("missing", "whatsapp", [])


def test_invalid_status_and_time_zone_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="time zone"):
        store.begin_import("whatsapp", "1", "h", "Mars/Olympus")
    with pytest.raises(ValueError, match="status"):
        store.finish_import(new_import(store), "running")  # type: ignore[arg-type]  # deliberately invalid


# -- query -----------------------------------------------------------------------------------


def test_query_is_parametrised(store: Store, make_message: MessageFactory) -> None:
    store.upsert(new_import(store), "whatsapp", [make_message(i) for i in range(5)])
    table = store.query(
        "WITH m AS (SELECT * FROM messages WHERE is_from_me = ?) SELECT count(*) AS n FROM m",
        [True],
    )
    assert table.to_pylist() == [{"n": 3}]
    assert store.query("FROM messages SELECT count(*) AS n").to_pylist() == [{"n": 5}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO schema_meta VALUES ('x', 'y')",
        "UPDATE messages SET text = 'x'",
        "DELETE FROM messages",
        "DROP TABLE messages",
        "CREATE TABLE t AS SELECT 1",
        "ALTER TABLE messages ADD COLUMN x INT",
        "COPY messages TO 'out.csv'",
        "ATTACH 'other.duckdb'",
        "SET threads = 1",
        "EXPLAIN SELECT 1",
        "WITH x AS (SELECT 1) INSERT INTO schema_meta SELECT 'a', 'b' FROM x",
        "SELECT 1; DELETE FROM messages",
        "SELECT 1; SELECT 2",
        "",
    ],
)
def test_query_rejects_anything_but_one_read(store: Store, sql: str) -> None:
    with pytest.raises(QueryNotAllowedError):
        store.query(sql)
    assert store.query("SELECT count(*) AS n FROM schema_meta").to_pylist() == [{"n": 1}]


# -- read-only -------------------------------------------------------------------------------


def test_open_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "life.duckdb"
    Store.open(path).close()
    assert path.is_file()


def test_read_only_missing_file_raises_clearly(tmp_path: Path) -> None:
    path = tmp_path / "nowhere" / "life.duckdb"
    with pytest.raises(FileNotFoundError, match="no sherd database"):
        Store.open(path, read_only=True)
    assert not path.parent.exists()


def test_read_only_store_cannot_write(db_path: Path, make_message: MessageFactory) -> None:
    with Store.open(db_path) as store:
        import_id = new_import(store)
        store.upsert(import_id, "whatsapp", [make_message(1)])

    with Store.open(db_path, read_only=True) as store:
        assert store.read_only
        assert store.table_counts()["messages"] == 1
        with pytest.raises(ReadOnlyStoreError):
            new_import(store)
        with pytest.raises(ReadOnlyStoreError):
            store.upsert(import_id, "whatsapp", [make_message(2)])
        with pytest.raises(ReadOnlyStoreError):
            store.finish_import(import_id, "succeeded")
        # The connection itself is read-only too, below the API checks.
        with pytest.raises(duckdb.InvalidInputException, match="read-only"):
            store._conn.execute("DELETE FROM messages")
        assert store.table_counts()["messages"] == 1


# -- the database enforces the contract without the models -----------------------------------

VALID_RAW: dict[str, dict[str, object]] = {
    "messages": {"chat_id": "c", "chat_kind": "direct", "is_from_me": True, "kind": "text"},
    "media_plays": {"media_kind": "track", "ms_played": 1},
    "transactions": {"amount": 1, "currency": "EUR", "account": "a", "kind": "card"},
    "events": {"kind": "x.y"},
    "locations": {"lat": 1.0, "lon": 2.0, "kind": "ping"},
}


def raw_insert(conn: duckdb.DuckDBPyConnection, table: str, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": "id-1",
        "source": "s",
        "source_file": "f",
        "source_row_id": "r",
        "import_id": "i",
        "imported_at": TS,
        "ts": TS,
        **VALID_RAW[table],
        **overrides,
    }
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(values.values()))


@pytest.fixture
def raw(db_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    Store.open(db_path).close()
    with duckdb.connect(str(db_path)) as conn:
        yield conn


@pytest.mark.parametrize("table", sorted(VALID_RAW))
def test_raw_valid_rows_accepted(raw: duckdb.DuckDBPyConnection, table: str) -> None:
    raw_insert(raw, table)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("messages", "chat_kind", "channel"),
        ("messages", "kind", "image"),
        ("messages", "media_type", "photo"),
        ("media_plays", "media_kind", "song"),
        ("transactions", "kind", "payment"),
        ("locations", "kind", "stop"),
    ],
)
def test_raw_check_constraints(
    raw: duckdb.DuckDBPyConnection, table: str, column: str, value: str
) -> None:
    with pytest.raises(duckdb.ConstraintException, match="CHECK"):
        raw_insert(raw, table, **{column: value})


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("messages", "is_from_me"),
        ("messages", "ts"),
        ("transactions", "amount"),
        ("locations", "lat"),
        ("events", "source_file"),
    ],
)
def test_raw_not_null_constraints(raw: duckdb.DuckDBPyConnection, table: str, column: str) -> None:
    with pytest.raises(duckdb.ConstraintException, match="NOT NULL"):
        raw_insert(raw, table, **{column: None})


def test_raw_unique_source_row_id(raw: duckdb.DuckDBPyConnection) -> None:
    raw_insert(raw, "events")
    with pytest.raises(duckdb.ConstraintException, match="source"):
        raw_insert(raw, "events", id="id-2")


def test_raw_import_status_check(raw: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.ConstraintException, match="CHECK"):
        raw.execute(
            "INSERT INTO imports (id, connector, connector_version, path_hash, tz, started_at,"
            " status) VALUES ('i', 'c', '1', 'h', 'UTC', ?, 'done')",
            [TS],
        )


def test_writable_reopen_fails_dangling_import(db_path: Path) -> None:
    with Store.open(db_path) as store:
        import_id = new_import(store)
    with Store.open(db_path, read_only=True) as store:
        assert store.query("SELECT status FROM imports").to_pylist() == [{"status": "running"}]
    with Store.open(db_path) as store:
        row = store.query("SELECT * FROM imports WHERE id = ?", [import_id]).to_pylist()[0]
        assert row["status"] == "failed"
        assert row["finished_at"] >= row["started_at"]


def test_read_only_store_rejects_select_side_effects(db_path: Path) -> None:
    with Store.open(db_path) as store:
        store._conn.execute("CREATE SEQUENCE probe_seq")
    with (
        Store.open(db_path, read_only=True) as store,
        pytest.raises(duckdb.InvalidInputException, match="read-only"),
    ):
        store.query("SELECT nextval('probe_seq')")


def test_upsert_returns_ledger_delta(store: Store, make_message: MessageFactory) -> None:
    import_id = new_import(store)
    first = store.upsert(import_id, "whatsapp", [make_message(1)] * 3, batch_size=2)
    second = store.upsert(import_id, "whatsapp", [make_message(1), make_message(2)])
    assert first == UpsertStats(3, 1)
    assert second == UpsertStats(2, 1)
    assert store.finish_import(import_id, "succeeded") == UpsertStats(
        first.seen + second.seen, first.inserted + second.inserted
    )


def test_read_only_checkpoint_leaves_file_and_data_unchanged(
    db_path: Path, make_message: MessageFactory
) -> None:
    with Store.open(db_path) as store:
        import_id = new_import(store)
        store.upsert(import_id, "whatsapp", [make_message(1)])
        store.finish_import(import_id, "succeeded")
    before = db_path.read_bytes()
    with Store.open(db_path, read_only=True) as store:
        data = store.query("SELECT * FROM messages").to_pylist()
        store.query("SELECT * FROM checkpoint()")
        assert store.query("SELECT * FROM messages").to_pylist() == data
        assert db_path.read_bytes() == before
    assert db_path.read_bytes() == before
