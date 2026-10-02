from pathlib import Path

import duckdb
import pytest
from sherd_agent import sql_guard
from sherd_agent.sql_guard import QueryTimeoutError, SqlRejectedError
from sherd_core import Store


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM messages", "DELETE"),
        ("INSERT INTO messages (id) VALUES ('x')", "INSERT"),
        ("UPDATE transactions SET amount = 0", "UPDATE"),
        ("DROP TABLE messages", "DROP"),
        ("CREATE TABLE t AS SELECT 1", "CREATE"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("SELECT 1; DROP TABLE messages", "exactly one statement"),
        ("ATTACH '/tmp/other.duckdb' AS other", "ATTACH"),
        ("COPY messages TO '/tmp/out.csv'", "COPY"),
        ("SET threads = 1", "SET"),
        ("PRAGMA version", "PRAGMA"),
        ("PRAGMA database_list", "PRAGMA"),
        ("INSTALL httpfs", "LOAD"),
        ("LOAD httpfs", "LOAD"),
        ("EXPORT DATABASE '/tmp/x'", "EXPORT"),
        ("CALL pragma_version()", "CALL"),
        ("DESCRIBE messages", "DESCRIBE"),
        ("SHOW TABLES", "SHOW"),
        ("", "empty"),
        (" ; ", "empty"),
        ("SELEC 1", "does not parse"),
    ],
)
def test_check_rejects(sql: str, reason: str) -> None:
    with pytest.raises(SqlRejectedError, match=reason):
        sql_guard.check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM messages",
        "select 1;",
        "  -- leading comment\n  SELECT 1",
        "/* block */ WITH a AS (SELECT 1 AS x) SELECT * FROM a",
        "(SELECT 1) UNION ALL (SELECT 2)",
        "FROM messages SELECT count(*)",
        "VALUES (1), (2)",
    ],
)
def test_check_accepts_reads(sql: str) -> None:
    assert sql_guard.check(sql) == sql.strip().rstrip(";").strip()


def test_trailing_line_comment_does_not_break_the_wrapper(demo_store: Store) -> None:
    result = sql_guard.run(demo_store, "SELECT 42 AS answer -- the answer")
    assert result.table.to_pylist() == [{"answer": 42}]


def test_file_and_network_functions_blocked_by_the_sandbox(demo_store: Store) -> None:
    # A SELECT that passes the guard still cannot reach files or URLs on a sandboxed store.
    for sql in (
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT * FROM read_text('/etc/hosts')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM 'https://example.com/data.csv'",
    ):
        sql_guard.check(sql)
        with pytest.raises(duckdb.PermissionException):
            sql_guard.run(demo_store, sql)


def test_row_budget_truncates_and_reports(demo_store: Store) -> None:
    result = sql_guard.run(demo_store, "SELECT * FROM range(50) r(i)", row_budget=10)
    assert result.table.num_rows == 10
    assert result.truncated
    exact = sql_guard.run(demo_store, "SELECT * FROM range(10) r(i)", row_budget=10)
    assert exact.table.num_rows == 10
    assert not exact.truncated


def test_default_row_budget_is_10000(demo_store: Store) -> None:
    result = sql_guard.run(demo_store, "SELECT * FROM range(20000) r(i)")
    assert sql_guard.ROW_BUDGET == 10_000
    assert result.table.num_rows == 10_000
    assert result.truncated


def test_order_survives_the_wrapper(demo_store: Store) -> None:
    result = sql_guard.run(
        demo_store, "SELECT i FROM range(100) r(i) ORDER BY i DESC LIMIT 5", row_budget=3
    )
    assert result.table.column("i").to_pylist() == [99, 98, 97]


def test_time_budget_interrupts_a_slow_cross_join(demo_store: Store) -> None:
    slow = "SELECT count(*) FROM range(100000000) a, range(100000000) b"
    with pytest.raises(QueryTimeoutError, match=r"longer than 0\.3 s"):
        sql_guard.run(demo_store, slow, time_budget_s=0.3)
    # The store is still usable after the interrupt.
    assert sql_guard.run(demo_store, "SELECT 1 AS one").table.to_pylist() == [{"one": 1}]


def test_writes_fail_on_the_read_only_store_even_past_the_guard(demo_db: Path) -> None:
    with Store.open(demo_db, read_only=True, sandboxed=True) as store, pytest.raises(duckdb.Error):
        store._conn.execute("DELETE FROM messages")
