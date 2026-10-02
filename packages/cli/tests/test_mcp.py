import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sherd_cli.commands import mcp
from sherd_cli.main import create_app
from typer.testing import CliRunner


def test_db_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    paths: list[Path] = []
    monkeypatch.setattr(mcp, "serve", paths.append)
    for name in ("life.duckdb", "demo.duckdb", "custom.duckdb"):
        (tmp_path / name).touch()
    runner = CliRunner()
    for args in ([], ["--demo"], ["--demo", "--db", str(tmp_path / "custom.duckdb")]):
        result = runner.invoke(create_app(), ["mcp", *args])
        assert result.exit_code == 0
        assert result.stdout == ""
    assert paths == [tmp_path / name for name in ("life.duckdb", "demo.duckdb", "custom.duckdb")]


def test_missing_database_stderr_only(tmp_path: Path) -> None:
    result = CliRunner().invoke(create_app(), ["mcp", "--db", str(tmp_path / "absent")])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "No database" in result.stderr


def test_stdio_stdout_contains_only_protocol_frames(tmp_path: Path) -> None:
    from sherd_core import Store

    path = tmp_path / "stdio.duckdb"
    with Store.open(path):
        pass
    frames: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "query", "arguments": {"sql": "SELECT 42 AS n"}},
        },
    ]
    replies = []
    with subprocess.Popen(
        [sys.executable, "-c", "from sherd_cli.main import main; main()", "mcp", "--db", str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        assert proc.stdin is not None
        assert proc.stdout is not None
        for frame in frames:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
            if "id" in frame:
                replies.append(json.loads(proc.stdout.readline()))
        proc.stdin.close()
        # communicate must not try to flush the now closed stream.
        proc.stdin = None
        trailing, _ = proc.communicate(timeout=30)
        assert trailing == ""
        assert proc.returncode == 0
    assert {reply["id"] for reply in replies} == {1, 2, 3}
    assert all(reply["jsonrpc"] == "2.0" and "result" in reply for reply in replies)
    initialize = next(reply for reply in replies if reply["id"] == 1)
    assert "personal" in initialize["result"]["instructions"]
    assert "single SELECT" in initialize["result"]["instructions"]
