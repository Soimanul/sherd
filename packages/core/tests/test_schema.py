"""The created database matches Contract A (PLAN §4.2) column for column."""

import re
from pathlib import Path

import duckdb
import pytest
from sherd_core import Store
from sherd_core.schema import SCHEMA_VERSION
from sherd_core.store import SchemaVersionError

TZ = "TIMESTAMP WITH TIME ZONE"
COMMON = [
    ("id", "VARCHAR", False),
    ("source", "VARCHAR", False),
    ("source_file", "VARCHAR", False),
    ("source_row_id", "VARCHAR", False),
    ("import_id", "VARCHAR", False),
    ("imported_at", TZ, False),
    ("meta", "JSON", True),
]

# (column, type, nullable) in table order, transcribed from PLAN §4.2.
EXPECTED_COLUMNS: dict[str, list[tuple[str, str, bool]]] = {
    "messages": [
        *COMMON,
        ("chat_id", "VARCHAR", False),
        ("chat_name", "VARCHAR", True),
        ("chat_kind", "VARCHAR", False),
        ("sender_id", "VARCHAR", True),
        ("sender_name", "VARCHAR", True),
        ("is_from_me", "BOOLEAN", False),
        ("contact_id", "VARCHAR", True),
        ("ts", TZ, False),
        ("text", "VARCHAR", True),
        ("kind", "VARCHAR", False),
        ("media_type", "VARCHAR", True),
        ("reply_to_id", "VARCHAR", True),
    ],
    "media_plays": [
        *COMMON,
        ("ts", TZ, False),
        ("media_kind", "VARCHAR", False),
        ("artist", "VARCHAR", True),
        ("track", "VARCHAR", True),
        ("album", "VARCHAR", True),
        ("uri", "VARCHAR", True),
        ("ms_played", "BIGINT", False),
        ("platform", "VARCHAR", True),
        ("shuffle", "BOOLEAN", True),
        ("skipped", "BOOLEAN", True),
    ],
    "transactions": [
        *COMMON,
        ("ts", TZ, False),
        ("amount", "DECIMAL(18,2)", False),
        ("currency", "VARCHAR", False),
        ("merchant_raw", "VARCHAR", True),
        ("merchant", "VARCHAR", True),
        ("counterparty", "VARCHAR", True),
        ("contact_id", "VARCHAR", True),
        ("category", "VARCHAR", True),
        ("account", "VARCHAR", False),
        ("balance", "DECIMAL(18,2)", True),
        ("kind", "VARCHAR", False),
    ],
    "events": [
        *COMMON,
        ("ts", TZ, False),
        ("kind", "VARCHAR", False),
        ("title", "VARCHAR", True),
        ("url", "VARCHAR", True),
    ],
    "locations": [
        *COMMON,
        ("ts", TZ, False),
        ("end_ts", TZ, True),
        ("lat", "DOUBLE", False),
        ("lon", "DOUBLE", False),
        ("accuracy_m", "DOUBLE", True),
        ("place_name", "VARCHAR", True),
        ("kind", "VARCHAR", False),
    ],
    "contacts": [
        ("id", "VARCHAR", False),
        ("display_name", "VARCHAR", False),
        ("aliases", "VARCHAR[]", False),
        ("identities", "VARCHAR[]", False),
        ("sources", "VARCHAR[]", False),
        ("merged_into", "VARCHAR", True),
        ("created_at", TZ, False),
        ("updated_at", TZ, False),
    ],
    "imports": [
        ("id", "VARCHAR", False),
        ("connector", "VARCHAR", False),
        ("connector_version", "VARCHAR", False),
        ("path_hash", "VARCHAR", False),
        ("tz", "VARCHAR", False),
        ("started_at", TZ, False),
        ("finished_at", TZ, True),
        ("rows_seen", "BIGINT", False),
        ("rows_inserted", "BIGINT", False),
        ("status", "VARCHAR", False),
    ],
    "schema_meta": [("key", "VARCHAR", False), ("value", "VARCHAR", True)],
}

FACT_TABLES = ["messages", "media_plays", "transactions", "events", "locations"]

EXPECTED_CHECKS: dict[tuple[str, str], set[str]] = {
    ("messages", "chat_kind"): {"direct", "group"},
    ("messages", "kind"): {"text", "media", "system", "deleted"},
    ("messages", "media_type"): {"image", "video", "audio", "document", "sticker", "gif", "other"},
    ("media_plays", "media_kind"): {"track", "episode", "video", "audiobook"},
    ("transactions", "kind"): {
        "card",
        "transfer",
        "fee",
        "topup",
        "exchange",
        "refund",
        "other",
    },
    ("locations", "kind"): {"ping", "visit", "segment"},
    ("imports", "status"): {"running", "succeeded", "failed"},
}


def _columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str, bool]]:
    rows = conn.execute(
        "SELECT column_name, data_type, is_nullable FROM duckdb_columns()"
        " WHERE schema_name = 'main' AND table_name = ? ORDER BY column_index",
        [table],
    ).fetchall()
    return [(str(name), str(type_), bool(nullable)) for name, type_, nullable in rows]


def _constraints(conn: duckdb.DuckDBPyConnection, kind: str) -> set[tuple[str, tuple[str, ...]]]:
    rows = conn.execute(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints()"
        " WHERE schema_name = 'main' AND constraint_type = ?",
        [kind],
    ).fetchall()
    return {(str(table), tuple(columns)) for table, columns in rows}


@pytest.fixture
def conn(db_path: Path) -> duckdb.DuckDBPyConnection:
    Store.open(db_path).close()
    return duckdb.connect(str(db_path), read_only=True)


def test_tables_are_exactly_contract_a(conn: duckdb.DuckDBPyConnection) -> None:
    tables = {
        str(name)
        for (name,) in conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
        ).fetchall()
    }
    assert tables == set(EXPECTED_COLUMNS)


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_columns_types_and_nullability(conn: duckdb.DuckDBPyConnection, table: str) -> None:
    assert _columns(conn, table) == EXPECTED_COLUMNS[table]


def test_primary_and_unique_keys(conn: duckdb.DuckDBPyConnection) -> None:
    assert _constraints(conn, "PRIMARY KEY") == {
        *((table, ("id",)) for table in [*FACT_TABLES, "contacts", "imports"]),
        ("schema_meta", ("key",)),
    }
    assert _constraints(conn, "UNIQUE") == {
        (table, ("source", "source_row_id")) for table in FACT_TABLES
    }


def test_check_constraints(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name, constraint_column_names, expression FROM duckdb_constraints()"
        " WHERE schema_name = 'main' AND constraint_type = 'CHECK'"
    ).fetchall()
    checks = {
        (str(table), columns[0]): set(re.findall(r"'([^']*)'", expression))
        for table, columns, expression in rows
    }
    assert all(len(columns) == 1 for _, columns, _ in rows)
    assert checks == EXPECTED_CHECKS


def test_import_counters_default_to_zero(conn: duckdb.DuckDBPyConnection) -> None:
    defaults = dict(
        conn.execute(
            "SELECT column_name, column_default FROM duckdb_columns()"
            " WHERE table_name = 'imports' AND column_default IS NOT NULL"
        ).fetchall()
    )
    assert defaults == {"rows_seen": "0", "rows_inserted": "0"}


def test_schema_version_recorded(conn: duckdb.DuckDBPyConnection) -> None:
    assert conn.execute("SELECT key, value FROM schema_meta").fetchall() == [
        ("schema_version", str(SCHEMA_VERSION))
    ]


def test_session_time_zone_is_utc(store: Store) -> None:
    assert store.query("SELECT current_setting('TimeZone') AS tz").to_pylist() == [{"tz": "UTC"}]


def test_reopen_does_not_migrate_again(db_path: Path) -> None:
    Store.open(db_path).close()
    with Store.open(db_path) as store:
        assert store.query("SELECT count(*) AS n FROM schema_meta").to_pylist() == [{"n": 1}]


def test_newer_schema_is_refused(db_path: Path) -> None:
    Store.open(db_path).close()
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", ["99"])
    with pytest.raises(SchemaVersionError, match="newer"):
        Store.open(db_path)
    with pytest.raises(SchemaVersionError, match="newer"):
        Store.open(db_path, read_only=True)


def test_read_only_open_does_not_migrate(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    duckdb.connect(str(db_path)).close()
    with pytest.raises(SchemaVersionError, match="writable"):
        Store.open(db_path, read_only=True)
