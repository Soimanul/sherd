"""The store: the only module that writes to the sherd database (PLAN §4.4)."""

import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import pyarrow as pa
import pydantic_core

from sherd_core.ids import row_id
from sherd_core.models import FACT_TABLES, ROW_MODELS, Row
from sherd_core.schema import MIGRATIONS, SCHEMA_VERSION, SESSION_SETUP

logger = logging.getLogger("sherd.core")

_BATCH_VIEW = "_sherd_upsert_batch"
_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")
_ARROW_TYPES: dict[str, pa.DataType] = {
    "VARCHAR": pa.string(),
    "JSON": pa.string(),
    "BOOLEAN": pa.bool_(),
    "BIGINT": pa.int64(),
    "DOUBLE": pa.float64(),
    "TIMESTAMP WITH TIME ZONE": pa.timestamp("us", tz="UTC"),
}


class StoreError(Exception):
    """Base class for store errors."""


class ReadOnlyStoreError(StoreError):
    """A write was attempted on a store opened with `read_only=True`."""


class SchemaVersionError(StoreError):
    """The database schema version cannot be used by this version of sherd."""


class QueryNotAllowedError(StoreError):
    """`Store.query()` was given something other than a single read statement."""


@dataclass(frozen=True)
class UpsertStats:
    seen: int
    inserted: int


@dataclass(frozen=True, eq=False)
class _Layout:
    """How one row model maps onto its table."""

    table: str
    fields: tuple[str, ...]
    schema: pa.Schema
    insert_sql: str


def _arrow_type(duckdb_type: str) -> pa.DataType:
    if match := _DECIMAL.fullmatch(duckdb_type):
        return pa.decimal128(int(match[1]), int(match[2]))
    return _ARROW_TYPES[duckdb_type]


class Store:
    """A sherd database. Create with `Store.open()`; not safe to share between threads."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, *, read_only: bool) -> None:
        self._conn = conn
        self._read_only = read_only
        self._layouts: dict[type[Row], _Layout] = {}

    @classmethod
    def open(cls, path: Path, *, read_only: bool = False) -> "Store":
        """Open the database at `path`; when writable, create it and migrate it if needed."""
        path = Path(path)
        if read_only and not path.is_file():
            raise FileNotFoundError(f"no sherd database at {path}; import something first")
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(path), read_only=read_only)
        try:
            for statement in SESSION_SETUP:
                conn.execute(statement)
            store = cls(conn, read_only=read_only)
            store._prepare_schema()
            store._load_layouts()
            if not read_only:
                conn.execute(
                    "UPDATE imports SET status = 'failed', finished_at = ?"
                    " WHERE status = 'running'",
                    [datetime.now(UTC)],
                )
        except BaseException:
            conn.close()
            raise
        logger.info("store opened: schema v%d, read_only=%s", SCHEMA_VERSION, read_only)
        return store

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def read_only(self) -> bool:
        return self._read_only

    def close(self) -> None:
        self._conn.close()

    # -- imports -------------------------------------------------------------------------------

    def begin_import(self, connector: str, connector_version: str, path_hash: str, tz: str) -> str:
        """Record a running import and return its id."""
        self._require_writable()
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"not an IANA time zone: {tz!r}") from error
        import_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO imports (id, connector, connector_version, path_hash, tz, started_at,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            [import_id, connector, connector_version, path_hash, tz, datetime.now(UTC)],
        )
        logger.info("import %s started: connector=%s", import_id, connector)
        return import_id

    def finish_import(self, import_id: str, status: Literal["succeeded", "failed"]) -> UpsertStats:
        """Close a running import and return its persisted row counts."""
        self._require_writable()
        if status not in ("succeeded", "failed"):
            raise ValueError(f"invalid import status: {status!r}")
        result = self._conn.execute(
            "UPDATE imports SET finished_at = ?, status = ?"
            " WHERE id = ? AND status = 'running' RETURNING rows_seen, rows_inserted",
            [datetime.now(UTC), status, import_id],
        ).fetchone()
        if result is None:
            raise StoreError(f"import {import_id} is not running")
        stats = UpsertStats(int(result[0]), int(result[1]))
        logger.info(
            "import %s %s: %d rows seen, %d inserted",
            import_id,
            status,
            stats.seen,
            stats.inserted,
        )
        return stats

    # -- writes --------------------------------------------------------------------------------

    def upsert(
        self, import_id: str, source: str, rows: Iterable[Row], batch_size: int = 5000
    ) -> UpsertStats:
        """Insert canonical rows; rows already present (same source + source_row_id) are kept.

        `rows` is consumed lazily and written in batches of at most `batch_size` per table. Each
        batch commits on its own (one transaction for a whole export would hold every row in
        memory), so if `rows` raises, the batches already written stay; re-running the import is
        safe because the first write wins.
        """
        self._require_writable()
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if not source:
            raise ValueError("source must be a connector id")
        self._require_running_import(import_id)
        params = {"source": source, "import_id": import_id, "imported_at": datetime.now(UTC)}
        buffers: dict[_Layout, dict[str, Row]] = {}
        batch_seen: dict[_Layout, int] = {}
        seen = inserted = 0
        for row in rows:
            layout = self._layouts.get(type(row))
            if layout is None:
                raise TypeError(f"not a canonical row: {type(row).__name__}")
            seen += 1
            buffer = buffers.setdefault(layout, {})
            buffer.setdefault(row_id(source, row.source_row_id), row)
            batch_seen[layout] = batch_seen.get(layout, 0) + 1
            if batch_seen[layout] >= batch_size:
                inserted += self._insert(layout, buffer, params, batch_seen[layout])
                buffer.clear()
                batch_seen[layout] = 0
        for layout, buffer in buffers.items():
            if buffer:
                inserted += self._insert(layout, buffer, params, batch_seen[layout])
        logger.info("import %s: upserted %d rows, %d new", import_id, seen, inserted)
        return UpsertStats(seen=seen, inserted=inserted)

    def _insert(
        self, layout: _Layout, buffer: dict[str, Row], params: dict[str, object], seen: int
    ) -> int:
        rows = buffer.values()
        columns: dict[str, list[object]] = {"id": list(buffer)}
        for name in layout.fields:
            values: list[object] = [getattr(row, name) for row in rows]
            if name == "meta":
                values = [None if v is None else pydantic_core.to_json(v).decode() for v in values]
            columns[name] = values
        batch = pa.Table.from_pydict(columns, schema=layout.schema)
        self._conn.register(_BATCH_VIEW, batch)
        try:
            self._conn.begin()
            try:
                result = self._conn.execute(layout.insert_sql, params).fetchone()
                inserted = int(result[0]) if result else 0
                self._conn.execute(
                    "UPDATE imports SET rows_seen = rows_seen + ?,"
                    " rows_inserted = rows_inserted + ? WHERE id = ?",
                    [seen, inserted, params["import_id"]],
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        finally:
            self._conn.unregister(_BATCH_VIEW)
        return inserted

    # -- reads ---------------------------------------------------------------------------------

    def query(self, sql: str, params: Sequence[object] = ()) -> pa.Table:
        """Run one parametrised read statement (SELECT, WITH … SELECT, FROM …).

        This is a mistake guard, not a security boundary; untrusted SQL must use
        Store.open(path, read_only=True).
        """
        statements = self._conn.extract_statements(sql)
        if len(statements) != 1:
            raise QueryNotAllowedError(f"expected one statement, got {len(statements)}")
        statement = statements[0]
        if statement.type != duckdb.StatementType.SELECT:
            raise QueryNotAllowedError(f"only reads are allowed, got {statement.type.name}")
        return self._conn.execute(statement, list(params)).to_arrow_table()

    def table_counts(self) -> dict[str, int]:
        """Rows per fact table."""
        union = " UNION ALL ".join(
            f"SELECT '{table}', count(*) FROM {table}" for table in FACT_TABLES
        )
        return {str(table): int(count) for table, count in self._conn.execute(union).fetchall()}

    # -- internals -----------------------------------------------------------------------------

    def _require_writable(self) -> None:
        if self._read_only:
            raise ReadOnlyStoreError("the store was opened read-only")

    def _require_running_import(self, import_id: str) -> None:
        row = self._conn.execute("SELECT status FROM imports WHERE id = ?", [import_id]).fetchone()
        if row is None or row[0] != "running":
            raise StoreError(f"import {import_id} is not running")

    def _schema_version(self) -> int:
        exists = self._conn.execute(
            "SELECT count(*) FROM duckdb_tables()"
            " WHERE database_name = current_database() AND schema_name = 'main'"
            " AND table_name = 'schema_meta'"
        ).fetchone()
        if not exists or exists[0] == 0:
            return 0
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0

    def _prepare_schema(self) -> None:
        version = self._schema_version()
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema v{version} is newer than this sherd (v{SCHEMA_VERSION})"
            )
        if version == SCHEMA_VERSION:
            return
        if self._read_only:
            raise SchemaVersionError(
                f"database schema v{version} needs migrating to v{SCHEMA_VERSION};"
                " open it writable first"
            )
        self._conn.begin()
        try:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    self._conn.execute(statement)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', ?)",
                [str(SCHEMA_VERSION)],
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        logger.info("schema migrated: v%d -> v%d", version, SCHEMA_VERSION)

    def _load_layouts(self) -> None:
        for model in ROW_MODELS:
            table = model.table_name
            types = dict(
                self._conn.execute(
                    "SELECT column_name, data_type FROM duckdb_columns()"
                    " WHERE database_name = current_database() AND schema_name = 'main'"
                    " AND table_name = ?",
                    [table],
                ).fetchall()
            )
            fields = tuple(model.model_fields)
            missing = [name for name in fields if name not in types]
            if missing:
                raise SchemaVersionError(f"{table} lacks columns {missing}")
            schema = pa.schema([(name, _arrow_type(types[name])) for name in ("id", *fields)])
            columns = ("id", "source", "import_id", "imported_at", *fields)
            values = ("id", "$source", "$import_id", "$imported_at", *fields)
            # `id` derives from (source, source_row_id), so targeting it covers both unique
            # constraints; an untargeted ON CONFLICT is two orders of magnitude slower.
            insert_sql = (
                f"INSERT INTO {table} ({', '.join(columns)})"
                f" SELECT {', '.join(values)} FROM {_BATCH_VIEW} ON CONFLICT (id) DO NOTHING"
            )
            self._layouts[model] = _Layout(table, fields, schema, insert_sql)
