"""Read-only stdio MCP tools, with a disclosure budget on every response."""

import asyncio
import base64
import json
import logging
import math
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
import pyarrow as pa
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError
from sherd_agent import sql_guard
from sherd_agent.ask import dig_params
from sherd_core import Store, load_catalog
from sherd_insights import registry

MAX_ROWS = 200
MAX_BYTES = 32 * 1024
MAX_CELL_CHARS = 500
MAX_SUMMARY_CHARS = 2000
BUDGET_DESCRIPTION = (
    " Responses contain at most 200 rows and 32 KB of JSON; cells are limited to 500 "
    "characters with … (narrative/text_summary: 2,000); truncated and row_count report "
    "disclosure cuts and the original count."
)
INSTRUCTIONS = (
    "This database contains personal data. Access is read-only. SQL must be a single SELECT "
    "(a SELECT with a WITH clause is allowed). Results may be forwarded to remote models; "
    "request only the data needed." + BUDGET_DESCRIPTION
)
logger = logging.getLogger("sherd.mcp")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def disclose(rows: list[dict[str, Any]], **metadata: Any) -> types.CallToolResult:
    """Bound cells, records and the serialized MCP result (including JSON escaping)."""
    cut = False
    non_finite = 0

    def clip(value: Any, limit: int = MAX_CELL_CHARS) -> Any:
        nonlocal cut, non_finite
        if isinstance(value, float) and not math.isfinite(value):
            non_finite += 1
            return None
        if isinstance(value, bytes):
            encoded = base64.b64encode(value).decode("ascii")
            if len(encoded) > limit:
                cut = True
                encoded = encoded[: limit // 4 * 4]
            return {"$blob": encoded, "bytes": len(value)}
        if isinstance(value, pa.MonthDayNano):
            seconds = Decimal(value.nanoseconds) / Decimal(1_000_000_000)
            value = f"P{value.months}M{value.days}DT{seconds:f}S"
        elif isinstance(value, datetime):
            value = (value.astimezone(UTC) if value.tzinfo else value).isoformat()
        elif isinstance(value, date | time):
            value = value.isoformat()
        elif isinstance(value, Decimal | UUID):
            value = str(value)
        if isinstance(value, dict):
            return {clip(str(k)): clip(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [clip(v) for v in value]
        if isinstance(value, str) and len(value) > limit:
            cut = True
            return value[: limit - 1] + "…"
        return value

    original = metadata.pop("row_count", len(rows))
    bounded_metadata = {
        key: clip(
            value, MAX_SUMMARY_CHARS if key in {"narrative", "text_summary"} else MAX_CELL_CHARS
        )
        for key, value in metadata.items()
    }
    payload = {
        **bounded_metadata,
        "rows": [],
        "row_count": original,
        "truncated": False,
        "non_finite": 0,
    }

    def result() -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=_json(payload))])

    # Reserve space for the JSON-RPC envelope; measure the SDK's actual serialized result.
    def fits() -> bool:
        return len(result().model_dump_json(exclude_none=True).encode()) <= MAX_BYTES - 256

    for row in rows[:MAX_ROWS]:
        # Nested values are a single cell too, not a way around the cell cap.
        bounded = {}
        for key, value in row.items():
            converted = clip(value)
            if isinstance(value, dict | list | tuple) and len(_json(converted)) > MAX_CELL_CHARS:
                converted = clip(_json(converted))
            bounded[clip(str(key))] = converted
        payload["non_finite"] = non_finite
        payload["rows"].append(bounded)
        payload["truncated"] = True
        if not fits():
            payload["rows"].pop()
            cut = True
            break
    payload["non_finite"] = non_finite
    payload["truncated"] = cut or len(payload["rows"]) < original
    if not fits():
        payload = {"rows": [], "row_count": original, "truncated": True, "non_finite": non_finite}
    return result()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def create_server(store: Store, *, time_budget_s: float = sql_guard.TIME_BUDGET_S) -> Server[Any]:
    """Create a session over an already read-only, sandboxed Store; caller owns its lifetime."""
    server: Server[Any] = Server("sherd", instructions=INSTRUCTIONS)
    catalog = load_catalog()
    definitions = {
        "list_tables": ("List table names, row counts and min/max ts.", {}),
        "describe": ("Describe columns, SQL types and catalog descriptions.", {"table": "string"}),
        "query": ("Run one read-only SELECT with a 10 s total budget.", {"sql": "string"}),
        "list_insights": ("List available digs: id, title and requires.", {}),
        "run_insight": (
            "Run a dig; headline, narrative, text_summary and data, no Vega spec.",
            {"id": "string", "params": "object"},
        ),
    }

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]  # SDK decorator lacks types.
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=description + BUDGET_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {key: {"type": kind} for key, kind in arguments.items()},
                    "required": [key for key in arguments if key != "params"],
                    "additionalProperties": False,
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True, destructiveHint=False, openWorldHint=False
                ),
            )
            for name, (description, arguments) in definitions.items()
        ]

    def tables() -> list[str]:
        return [
            str(row["table_name"])
            for row in store.query(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
                "AND schema_name = 'main' ORDER BY table_name"
            ).to_pylist()
        ]

    # Disable SDK validation errors that echo unbounded input. Validate locally and return
    # bounded, non-personal errors; the SDK still advertises the full input schemas.
    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]  # SDK decorator lacks types.
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name not in definitions:
                raise ValueError("unknown tool")
            expected = definitions[name][1]
            if set(arguments) - set(expected) or any(
                key not in arguments or not isinstance(arguments[key], str)
                for key in expected
                if key != "params"
            ):
                raise ValueError("invalid arguments")
            rows: list[dict[str, Any]]
            if name == "list_tables":
                rows = []
                for table in tables():
                    has_ts = store.query(
                        "SELECT count(*) AS n FROM information_schema.columns "
                        "WHERE table_catalog = current_database() AND table_schema = 'main' "
                        "AND table_name = ? AND column_name = 'ts'",
                        [table],
                    ).to_pylist()[0]["n"]
                    span = (
                        ", min(ts) AS min_ts, max(ts) AS max_ts"
                        if has_ts
                        else ", NULL AS min_ts, NULL AS max_ts"
                    )
                    stats = store.query(
                        f"SELECT count(*) AS row_count{span} FROM main.{_quote(table)}"
                    ).to_pylist()[0]
                    rows.append({"table": table, **stats})
                return disclose(rows)
            if name == "describe":
                table = arguments["table"]
                if table not in tables():
                    raise ValueError("unknown table")
                doc = catalog.tables.get(table)
                rows = store.query(
                    "SELECT column_name AS name, data_type AS type FROM information_schema.columns "
                    "WHERE table_catalog = current_database() AND table_schema = 'main' "
                    "AND table_name = ? ORDER BY ordinal_position",
                    [table],
                ).to_pylist()
                for row in rows:
                    row["description"] = doc.columns.get(row["name"], "") if doc else ""
                return disclose(rows, description=doc.description if doc else "")
            if name == "query":
                sql = sql_guard.check(arguments["sql"])
                # Use the final column position so user aliases cannot collide with the count.
                data = sql_guard.run(
                    store,
                    f"SELECT preview.*, count(*) OVER () FROM (\n{sql}\n) AS preview",
                    row_budget=MAX_ROWS,
                    time_budget_s=time_budget_s,
                ).table
                count = data.column(-1)[0].as_py() if data.num_rows else 0
                preview = data.select(list(range(data.num_columns - 1)))
                return disclose(preview.to_pylist(), row_count=count)
            digs = {dig.id: dig for dig in registry.available(store)}
            if name == "list_insights":
                return disclose(
                    [{"id": d.id, "title": d.title, "requires": d.requires} for d in digs.values()]
                )
            if arguments["id"] not in digs:
                raise ValueError("unknown or unavailable insight")
            raw = arguments.get("params", {})
            if not isinstance(raw, dict):
                raise ValueError("params must be an object")
            computed = digs[arguments["id"]].compute(store, dig_params(raw, "UTC"))
            return disclose(
                computed.data.slice(0, MAX_ROWS).to_pylist(),
                row_count=computed.data.num_rows,
                headline=asdict(computed.headline) if computed.headline else None,
                narrative=computed.narrative,
                text_summary=computed.text_summary,
            )
        except (ValueError, ValidationError, duckdb.Error, sql_guard.QueryTimeoutError) as error:
            logger.info("%s %s", name, type(error).__name__)
            message = (
                "query exceeded the time budget"
                if isinstance(error, sql_guard.QueryTimeoutError)
                else "request rejected: invalid arguments or prohibited/unavailable data access"
            )
            result = disclose([], error=message)
            result.isError = True
            return result
        except Exception as error:
            logger.info("%s %s", name, type(error).__name__)
            result = disclose([], error=type(error).__name__, message="tool execution failed")
            result.isError = True
            return result

    return server


async def _serve(path: Path) -> None:
    with Store.open(path, read_only=True, sandboxed=True) as store:
        server = create_server(store)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())


class _SafeDiagnostics(logging.Filter):
    """Third-party diagnostics can include SQL or validation inputs: retain only severity."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "MCP diagnostic: %s"
        record.args = (record.levelname,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def serve(path: Path) -> None:
    """Run only the SDK's stdio transport. All diagnostics go to stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    asyncio.run(_serve(path))
