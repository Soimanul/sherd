"""Textual pilots exercise the same keyboard routes as the terminal user."""

import asyncio
from pathlib import Path

import pytest
from sherd_cli.commands.demo import build
from sherd_cli.main import create_app
from sherd_cli.tui import DOMAINS, Dashboard
from sherd_core import Store
from textual.widgets import DataTable, OptionList, Static
from typer.testing import CliRunner


def test_demo_navigation_and_detail(tmp_path: Path) -> None:
    path = tmp_path / "demo.duckdb"
    build(path)
    before = path.read_bytes()

    async def drive() -> None:
        with Store.open(path, read_only=True) as store:
            app = Dashboard(store)
            async with app.run_test(size=(100, 35)) as pilot:
                for index, name in enumerate(DOMAINS):
                    if index:
                        await pilot.press("left", "down")
                    assert app.selected_domain == name
                    choices = app.query_one("#headlines", OptionList)
                    if choices.option_count:
                        await pilot.press("enter", "enter")
                        assert app.selected_dig is not None
                        table = app.query_one("#data", DataTable)
                        assert table.row_count > 0
                        data = app.results[app.selected_dig].data
                        assert [str(cell) for cell in table.get_row_at(0)] == [
                            str(data[name][0].as_py()) for name in data.column_names
                        ]
                        assert str(app.query_one("#narrative", Static).content) == (
                            app.results[app.selected_dig].narrative
                        )
                await pilot.press("?")
                assert app.query_one("#help", Static).display
                await pilot.press("?")
                assert not app.query_one("#help", Static).display
                await pilot.press("q")

    asyncio.run(drive())
    assert path.read_bytes() == before


def test_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    with Store.open(path):
        pass

    async def drive() -> None:
        with Store.open(path, read_only=True) as store:
            app = Dashboard(store)
            async with app.run_test() as pilot:
                assert "No data here yet" in str(app.query_one("#narrative", Static).content)
                assert app.query_one("#headlines", OptionList).option_count == 0
                await pilot.press("right", "enter", "left", "down")
                assert app.selected_domain == "Music"
                assert app.query_one("#data", DataTable).row_count == 0

    asyncio.run(drive())


def test_requires_read_only_store(tmp_path: Path) -> None:
    with (
        Store.open(tmp_path / "db.duckdb") as store,
        pytest.raises(ValueError, match="read-only"),
    ):
        Dashboard(store)


def test_command_passes_read_only_demo_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "demo.duckdb"
    with Store.open(path):
        pass
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    calls: list[bool] = []

    def run(app: Dashboard) -> None:
        calls.append(app.store.read_only)

    monkeypatch.setattr(Dashboard, "run", run)
    result = CliRunner().invoke(create_app(), ["tui", "--demo"])
    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_compute_error_tile_preserves_navigation_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow as pa
    from sherd_insights import DigParams, DigResult, registry
    from sherd_insights.base import Dig

    class Broken:
        id = "messages.broken"
        title = "Broken insight"

        def __init__(self) -> None:
            self.requires: list[str] = []

        def compute(self, store: Store, params: DigParams) -> DigResult:
            raise RuntimeError("private synthetic detail")

    class Healthy:
        id = "messages.healthy"
        title = "Healthy insight"

        def __init__(self) -> None:
            self.requires: list[str] = []

        def compute(self, store: Store, params: DigParams) -> DigResult:
            return DigResult(pa.table({"value": [42]}), None, "Healthy narrative", None, "42")

    digs: list[Dig] = [Broken(), Healthy()]
    monkeypatch.setattr(registry, "available", lambda store: digs)
    path = tmp_path / "empty.duckdb"
    with Store.open(path):
        pass

    async def drive() -> None:
        with Store.open(path, read_only=True) as store:
            app = Dashboard(store)
            async with app.run_test() as pilot:
                choices = app.query_one("#headlines", OptionList)
                assert choices.option_count == 2
                prompt = str(choices.get_option_at_index(0).prompt)
                assert prompt == "Broken insight\nError: RuntimeError"
                await pilot.press("right", "enter")
                assert str(app.query_one("#narrative", Static).content) == "Error: RuntimeError"
                assert app.query_one("#data", DataTable).row_count == 0
                await pilot.press("down", "enter")
                assert str(app.query_one("#narrative", Static).content) == "Healthy narrative"
                assert str(app.query_one("#data", DataTable).get_row_at(0)[0]) == "42"
                await pilot.press("left", "down", "up", "q")

    asyncio.run(drive())
