import json
import time
from collections.abc import Iterator
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from sherd_cli.main import create_app
from sherd_connectors import registry
from sherd_connectors.base import Connector, DetectResult, ImportContext
from sherd_core import Event, Row, Store
from typer.testing import CliRunner, Result


class FixtureConnector:
    """A test connector whose single fixture variant holds two shell events."""

    id = "fixture_lines"
    version = "1"
    display_name = "Fixture lines"

    def __init__(self, root: Path) -> None:
        self.root = root

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(0.0, "test only")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        for file in sorted(path.iterdir()):
            for number, line in enumerate(file.read_text().splitlines()):
                yield Event(
                    source_file=file.relative_to(ctx.export_root).as_posix(),
                    source_row_id=f"{file.name}:{number}",
                    ts=datetime.fromisoformat(line).replace(tzinfo=ctx.tz),
                    kind="shell.command",
                    title="ls",
                )

    def fixtures(self) -> list[Path]:
        return [self.root / "basic"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    variant = tmp_path / "fixtures" / "fixture_lines" / "basic"
    (variant / "export").mkdir(parents=True)
    (variant / "export" / "history").write_text("2024-03-01T09:00:00\n2024-03-01T09:05:00\n")
    (variant / "meta.json").write_text(
        json.dumps({"tz": "Asia/Tokyo", "self_identities": [], "description": "two lines"})
    )
    found: dict[str, Connector] = {"fixture_lines": FixtureConnector(variant.parent)}
    monkeypatch.setattr(registry, "discover", lambda: found)
    path = tmp_path / "home"
    monkeypatch.setenv("SHERD_HOME", str(path))
    return path


def run(*args: str) -> Result:
    return CliRunner().invoke(create_app(), ["demo", *args])


def test_version_matches_installed_distribution() -> None:
    result = CliRunner().invoke(create_app(), ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"sherd {version('sherd-cli')}"


def imports(db: Path) -> list[dict[str, object]]:
    with Store.open(db, read_only=True) as store:
        result: list[dict[str, object]] = store.query(
            "SELECT connector, tz, status, rows_inserted FROM imports ORDER BY started_at"
        ).to_pylist()
    return result


def counts(db: Path) -> dict[str, int]:
    with Store.open(db, read_only=True) as store:
        return store.table_counts()


def test_demo_builds_from_fixtures_and_synth(home: Path) -> None:
    start = time.perf_counter()
    result = run()
    seconds = time.perf_counter() - start
    assert result.exit_code == 0, result.output
    assert seconds < 30
    db = home / "demo.duckdb"
    assert [(i["connector"], i["tz"], i["status"]) for i in imports(db)] == [
        ("fixture_lines", "Asia/Tokyo", "succeeded"),
        ("synth", "Europe/Bucharest", "succeeded"),
    ]
    table = counts(db)
    assert 40_000 < sum(table.values()) < 60_000
    for name, count in table.items():
        assert name in result.stdout
        assert f"{count:,}" in result.stdout
    assert f"{sum(table.values()):,} new rows" in result.stdout


def test_demo_without_source_tree_fixtures(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoInstalledFixtures(FixtureConnector):
        def fixtures(self) -> list[Path]:
            raise FileNotFoundError("source-tree fixtures are absent")

    monkeypatch.setattr(
        registry, "discover", lambda: {"fixture_lines": NoInstalledFixtures(home.parent)}
    )
    result = run()
    assert result.exit_code == 0, result.output
    assert "using synthetic demo data" in result.stdout
    assert 40_000 < sum(counts(home / "demo.duckdb").values()) < 60_000


def test_second_run_inserts_nothing_and_force_rebuilds(home: Path) -> None:
    db = home / "demo.duckdb"
    assert run().exit_code == 0
    before = counts(db)

    again = run()
    assert again.exit_code == 0, again.output
    assert "0 new rows" in again.stdout
    assert "use --force to rebuild" in again.stdout
    assert counts(db) == before
    assert {i["rows_inserted"] for i in imports(db)[2:]} == {0}

    forced = run("--force")
    assert forced.exit_code == 0, forced.output
    assert f"{sum(before.values()):,} new rows" in forced.stdout
    assert counts(db) == before
    assert len(imports(db)) == 2


def test_demo_db_option(home: Path, tmp_path: Path) -> None:
    db = tmp_path / "elsewhere" / "my-demo.duckdb"
    result = run("--db", str(db))
    assert result.exit_code == 0, result.output
    assert db.is_file()
    assert not (home / "demo.duckdb").exists()


@pytest.mark.parametrize("force", [[], ["--force"]], ids=["build", "force"])
def test_demo_error_is_one_line(home: Path, tmp_path: Path, force: list[str]) -> None:
    folder = tmp_path / "a-folder"
    folder.mkdir()
    result = run("--db", str(folder), *force)
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
    assert "Traceback" not in result.output
