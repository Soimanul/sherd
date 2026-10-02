import json
from datetime import datetime
from pathlib import Path

import pytest
from sherd_cli.main import create_app
from sherd_core import Message, Store
from typer.testing import CliRunner, Result


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Europe/Bucharest")
    with Store.open(tmp_path / "life.duckdb") as store:
        key = store.begin_import("synthetic", "1", "synthetic", "UTC")
        store.upsert(
            key,
            "synthetic",
            [
                Message(
                    source_file="synthetic.txt",
                    source_row_id="1",
                    chat_id="a",
                    chat_name="Contact A",
                    chat_kind="direct",
                    is_from_me=True,
                    ts=datetime.fromisoformat("2024-01-31T22:30:00+00:00"),
                    text="hello 😀",
                    kind="text",
                )
            ],
        )
        store.finish_import(key, "succeeded")
    return tmp_path


def run(*args: str) -> Result:
    return CliRunner().invoke(create_app(), ["show", *args])


def test_list_available_with_headlines(home: Path) -> None:
    got = run()
    assert got.exit_code == 0, got.output
    assert "messages.volume_by_contact" in got.stdout
    assert "Contact A: 1 messages" in got.stdout
    assert "Night-owl index" in got.stdout


def test_single_rich_table(home: Path) -> None:
    got = run("messages.volume_by_contact", "--top", "1")
    assert got.exit_code == 0, got.output
    assert "Contact A led with 1 messages." in got.stdout
    assert "bucket" in got.stdout
    assert "2024-02-01" in got.stdout


def test_full_json_local_zone_date_filters_and_options(home: Path) -> None:
    got = run(
        "messages.volume_by_contact",
        "--json",
        "--from",
        "2024-02-01",
        "--to",
        "2024-02-01",
        "--granularity",
        "day",
        "--top",
        "1",
    )
    assert got.exit_code == 0, got.output
    data = json.loads(got.stdout)
    assert set(data) == {"data", "chart", "narrative", "headline", "text_summary"}
    assert data["data"] == [{"contact": "Contact A", "bucket": "2024-02-01", "messages": 1}]
    assert data["headline"] == {"label": "Contact A", "value": 1, "unit": "messages", "delta": None}
    utc = run("messages.volume_by_contact", "--json", "--tz", "UTC")
    assert json.loads(utc.stdout)["data"][0]["bucket"] == "2024-01-01"


def test_missing_db_one_line_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    got = run()
    assert got.exit_code == 1
    assert got.stderr.splitlines() == ["No database yet. Run `sherd demo` or `sherd dig PATH`."]
    assert not (tmp_path / "life.duckdb").exists()


def test_index_markdown_without_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    got = run("--list", "--markdown")
    assert got.exit_code == 0
    assert got.stdout.startswith("| id | title | requires |")
    assert "| messages.emoji_words | Your emoji and words by year | messages |" in got.stdout
    assert run("--list").exit_code == 0


def test_demo_and_explicit_db(home: Path, tmp_path: Path) -> None:
    (home / "life.duckdb").rename(home / "demo.duckdb")
    assert run("--demo").exit_code == 0
    assert run("--db", str(home / "demo.duckdb")).exit_code == 0
    with Store.open(tmp_path / "empty.duckdb"):
        pass
    assert run("--db", str(tmp_path / "empty.duckdb")).stdout == ""


@pytest.mark.parametrize(
    "args",
    [
        ["missing"],
        ["--from", "invalid"],
        ["--from", "2024-02-02", "--to", "2024-01-01"],
        ["--granularity", "hour"],
        ["--tz", "bad/zone"],
        ["--top", "0"],
    ],
)
def test_invalid_arguments_are_friendly(home: Path, args: list[str]) -> None:
    got = run(*args)
    assert got.exit_code != 0
    assert "Traceback" not in got.output


def test_invalid_zone_reports_error(home: Path) -> None:
    got = run("--tz", "bad/zone")
    assert got.exit_code == 1
    assert got.stderr.startswith("error:")
    assert "bad/zone" in got.stderr
