"""Checks and runs model-written SQL: one read statement, a row budget and a time budget.

The guard is the first fence; the second is the store itself, which must be opened with
`Store.open(path, read_only=True, sandboxed=True)` (no writes, no file or network access, no
configuration changes).
"""

import re
import threading
import time
from dataclasses import dataclass

import duckdb
import pyarrow as pa
from sherd_core import Store

ROW_BUDGET = 10_000
TIME_BUDGET_S = 10.0
READ_KEYWORDS = frozenset({"select", "with", "from", "values"})
_LEADING_NOISE = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|\(", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")


class SqlRejectedError(ValueError):
    """The SQL is not a single read statement."""


class QueryTimeoutError(Exception):
    """The query ran past the time budget and was interrupted."""


@dataclass(frozen=True)
class QueryResult:
    sql: str
    table: pa.Table
    truncated: bool
    elapsed_s: float


def _first_keyword(sql: str) -> str:
    position = 0
    while match := _LEADING_NOISE.match(sql, position):
        position = match.end()
    word = _FIRST_WORD.match(sql, position)
    return word[0].lower() if word else ""


def check(sql: str) -> str:
    """Return `sql` without trailing semicolons if it is exactly one SELECT/WITH read."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlRejectedError("the query is empty")
    with duckdb.connect(":memory:", config={"enable_external_access": False}) as parser:
        try:
            statements = parser.extract_statements(sql)
        except duckdb.Error as error:
            raise SqlRejectedError(f"the query does not parse: {error}") from None
    if len(statements) != 1:
        raise SqlRejectedError(f"exactly one statement is allowed, got {len(statements)}")
    kind = statements[0].type
    if kind != duckdb.StatementType.SELECT:
        raise SqlRejectedError(f"only SELECT/WITH reads are allowed, got {kind.name}")
    keyword = _first_keyword(sql)
    if keyword not in READ_KEYWORDS:
        raise SqlRejectedError(
            f"only SELECT/WITH reads are allowed, got {keyword.upper() or 'nothing'}"
        )
    return sql


def wrap(sql: str, row_budget: int = ROW_BUDGET) -> str:
    """Cap the rows; newlines keep a trailing line comment in `sql` from eating the wrapper."""
    return f"SELECT * FROM (\n{sql}\n) AS sherd_query LIMIT {row_budget + 1}"


def run(
    store: Store,
    sql: str,
    *,
    row_budget: int = ROW_BUDGET,
    time_budget_s: float = TIME_BUDGET_S,
) -> QueryResult:
    """Check and run `sql`; raises SqlRejectedError, QueryTimeoutError or duckdb.Error."""
    sql = check(sql)
    timer = threading.Timer(time_budget_s, store.interrupt)
    started = time.monotonic()
    timer.start()
    try:
        table = store.query(wrap(sql, row_budget))
    except duckdb.InterruptException:
        raise QueryTimeoutError(
            f"the query took longer than {time_budget_s:g} s and was stopped"
        ) from None
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started
    truncated = table.num_rows > row_budget
    if truncated:
        table = table.slice(0, row_budget)
    return QueryResult(sql, table, truncated, elapsed)
