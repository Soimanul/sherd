import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_connected_server_and_client_session
from sherd_cli.commands.demo import build
from sherd_core import Store
from sherd_mcp.server import MAX_BYTES, create_server, disclose


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-demo") / "demo.duckdb"
    build(path)
    return path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(demo_db: Path) -> AsyncIterator[ClientSession]:
    with Store.open(demo_db, read_only=True, sandboxed=True) as store:
        async with create_connected_server_and_client_session(create_server(store)) as session:
            yield session


def payload(result: types.CallToolResult) -> dict[str, Any]:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    data: dict[str, Any] = json.loads(content.text)
    return data


@pytest.mark.anyio
async def test_five_tools_on_demo(
    client: ClientSession, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "list_tables",
        "describe",
        "query",
        "list_insights",
        "run_insight",
    }
    assert all("200 rows" in (tool.description or "") for tool in tools)
    tables = payload(await client.call_tool("list_tables"))["rows"]
    messages = next(row for row in tables if row["table"] == "messages")
    assert messages["row_count"] > 0
    assert messages["min_ts"] <= messages["max_ts"]
    columns = payload(await client.call_tool("describe", {"table": "messages"}))["rows"]
    assert any(row["name"] == "ts" and row["description"] and row["type"] for row in columns)
    count = payload(await client.call_tool("query", {"sql": "SELECT count(*) AS n FROM messages"}))
    assert count["rows"] == [{"n": messages["row_count"]}]
    assert count["row_count"] == 1
    assert not count["truncated"]
    digs = payload(await client.call_tool("list_insights"))["rows"]
    assert any(
        dig["id"] == "messages.volume_by_contact" and dig["requires"] == ["messages"]
        for dig in digs
    )
    result = await client.call_tool(
        "run_insight",
        {"id": "messages.volume_by_contact", "params": {"top_n": 3, "date_from": "2025-01-01"}},
    )
    assert not result.isError
    insight = payload(result)
    assert insight["rows"]
    assert insight["headline"]
    assert insight["narrative"]
    assert insight["text_summary"]
    assert "chart" not in insight
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM messages",
        "SELECT 1; SELECT 2",
        "COPY messages TO '/tmp/mcp.csv'",
        "SELECT * FROM read_csv('/tmp/secret.csv')",
        "SELECT * FROM '/tmp/secret.csv'",
        "SELECT * FROM read_parquet('/tmp/secret.parquet')",
        "SELECT * FROM read_csv('https://example.com/data.csv')",
    ],
)
async def test_query_rejects_unsafe(client: ClientSession, sql: str) -> None:
    result = await client.call_tool("query", {"sql": sql})
    assert result.isError
    assert payload(result)["rows"] == []


@pytest.mark.anyio
async def test_timeout(demo_db: Path) -> None:
    with Store.open(demo_db, read_only=True, sandboxed=True) as store:
        async with create_connected_server_and_client_session(
            create_server(store, time_budget_s=0.01)
        ) as client:
            result = await client.call_tool(
                "query",
                {"sql": "SELECT sum(a.i * b.i) FROM range(10000000) a(i), range(10000000) b(i)"},
            )
            assert result.isError
            assert "time budget" in payload(result)["error"]
            assert not (await client.call_tool("query", {"sql": "SELECT 1"})).isError


@pytest.mark.anyio
async def test_row_and_cell_budgets(client: ClientSession) -> None:
    result = await client.call_tool(
        "query", {"sql": "SELECT i, repeat('x', 600) AS cell FROM range(12001) t(i)"}
    )
    data = payload(result)
    assert data["row_count"] == 12001
    assert data["truncated"]
    assert 0 < len(data["rows"]) <= 200
    assert all(len(row["cell"]) == 500 and row["cell"].endswith("…") for row in data["rows"])
    assert len(result.model_dump_json().encode()) <= MAX_BYTES
    rows_only = payload(await client.call_tool("query", {"sql": "SELECT i FROM range(201) t(i)"}))
    assert len(rows_only["rows"]) == 200
    assert rows_only["row_count"] == 201
    assert rows_only["truncated"]


def test_byte_cap_includes_escaping_and_metadata() -> None:
    result = disclose([{str(i): '\\"' * 300 for i in range(100)}], narrative="é" * 600)
    assert len(result.model_dump_json().encode()) <= MAX_BYTES
    assert payload(result)["truncated"]
    nested = payload(disclose([{"nested": ["x" * 600]}]))
    assert len(nested["rows"][0]["nested"]) == 500
    assert nested["truncated"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params",
    [
        {"top_n": 0},
        {"unknown": True},
        {"tz": "Invalid/Zone"},
        {"date_from": "2026-01-01", "date_to": "2025-01-01"},
        {"granularity": "hour"},
    ],
)
async def test_invalid_dig_params(client: ClientSession, params: dict[str, Any]) -> None:
    assert (
        await client.call_tool(
            "run_insight", {"id": "messages.volume_by_contact", "params": params}
        )
    ).isError


@pytest.mark.anyio
async def test_errors_are_bounded_and_do_not_echo_input(client: ClientSession) -> None:
    result = await client.call_tool("describe", {"table": "personal" * 10000})
    assert result.isError
    assert "personal" not in payload(result)["error"]
    assert (await client.call_tool("describe", {"table": "messages", "extra": True})).isError
    assert (await client.call_tool("run_insight", {"id": "missing"})).isError


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("'ab'::BLOB", {"$blob": "YWI=", "bytes": 2}),
        ("TIME '10:00'", "10:00:00"),
        ("DATE '2025-01-01'", "2025-01-01"),
        ("TIMESTAMP '2025-01-01 10:00:00'", "2025-01-01T10:00:00"),
        ("TIMESTAMPTZ '2025-01-01 03:00:00+03'", "2025-01-01T00:00:00+00:00"),
        ("12.34::DECIMAL(18,2)", "12.34"),
        ("INTERVAL '1 month 2 days 3 seconds'", "P1M2DT3S"),
        ("[1,2]", [1, 2]),
        ("{'a': [1,2]}", {"a": [1, 2]}),
        ("MAP([1,2],['a','b'])", [[1, "a"], [2, "b"]]),
        ("'00000000-0000-0000-0000-000000000001'::UUID", "00000000-0000-0000-0000-000000000001"),
    ],
    ids=[
        "blob",
        "time",
        "date",
        "timestamp",
        "timestamptz",
        "decimal",
        "interval",
        "list",
        "struct",
        "map",
        "uuid",
    ],
)
async def test_query_type_serialization(
    client: ClientSession, expression: str, expected: Any
) -> None:
    result = await client.call_tool("query", {"sql": f"SELECT {expression} AS v"})
    assert not result.isError
    assert payload(result)["rows"] == [{"v": expected}]


@pytest.mark.anyio
async def test_query_non_finite_recursive(client: ClientSession) -> None:
    result = await client.call_tool(
        "query", {"sql": "SELECT ['NaN'::DOUBLE, 'inf'::DOUBLE, '-inf'::DOUBLE] AS v"}
    )
    assert not result.isError
    assert payload(result)["rows"] == [{"v": [None, None, None]}]
    assert payload(result)["non_finite"] == 3


def test_blob_cap_and_summary_cap() -> None:
    import base64
    from uuid import UUID

    result = payload(
        disclose(
            [{"v": b"x" * 1000, "uuid": UUID(int=1)}], narrative="x" * 1500, text_summary="y" * 2500
        )
    )
    assert len(result["rows"][0]["v"]["$blob"]) == 500
    assert len(base64.b64decode(result["rows"][0]["v"]["$blob"])) == 375
    assert result["rows"][0]["v"]["bytes"] == 1000
    assert result["rows"][0]["uuid"] == str(UUID(int=1))
    assert len(result["narrative"]) == 1500
    assert len(result["text_summary"]) == 2000
    assert result["truncated"]


@pytest.mark.anyio
async def test_unexpected_dig_error_is_safe(
    client: ClientSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from sherd_insights import registry

    class BrokenDig:
        id = "broken"

        def compute(self, *args: Any) -> Any:
            raise KeyError("private data " * 10000)

    monkeypatch.setattr(registry, "available", lambda store: [BrokenDig()])
    with caplog.at_level(logging.INFO, logger="sherd.mcp"):
        result = await client.call_tool("run_insight", {"id": "broken"})
    assert result.isError
    assert payload(result)["error"] == "KeyError"
    assert payload(result)["message"] == "tool execution failed"
    assert "private" not in result.model_dump_json() + caplog.text
    assert "run_insight KeyError" in caplog.text
    assert len(result.model_dump_json().encode()) < MAX_BYTES


@pytest.mark.anyio
@pytest.mark.parametrize("size", [0, 1, 201])
async def test_query_single_execution(
    demo_db: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    calls: list[str] = []
    original = Store.query

    def tracked(self: Store, sql: str, params: Any = ()) -> Any:
        calls.append(sql)
        return original(self, sql, params)

    monkeypatch.setattr(Store, "query", tracked)
    with Store.open(demo_db, read_only=True, sandboxed=True) as store:
        async with create_connected_server_and_client_session(create_server(store)) as client:
            result = await client.call_tool(
                "query", {"sql": f"SELECT uuid() AS v, 1 AS n FROM range({size})"}
            )
    assert not result.isError
    assert len(calls) == 1
    assert payload(result)["row_count"] == size
    assert len(payload(result)["rows"]) == min(size, 200)
