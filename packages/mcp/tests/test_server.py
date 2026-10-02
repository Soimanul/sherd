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
