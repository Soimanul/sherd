import sys
from pathlib import Path

import pytest
from sherd_cli import commands
from sherd_cli.main import app, create_app
from typer.testing import CliRunner


def test_version_output() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output == "sherd 0.0.0\n"


def test_discovery_registers_version() -> None:
    discovered = create_app()
    assert "version" in {
        command.name or command.callback.__name__
        for command in discovered.registered_commands
        if command.callback is not None
    }


def test_module_without_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "no_register.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(commands, "__path__", [*commands.__path__, str(tmp_path)])
    discovered = create_app()
    result = CliRunner().invoke(discovered, ["version"])
    assert result.exit_code == 0
    assert result.output == "sherd 0.0.0\n"


def test_discovery_runs_temporary_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "temporary_command.py").write_text(
        "import typer\n"
        "def register(app):\n"
        "    @app.command()\n"
        "    def temporary():\n"
        "        typer.echo('discovered command ran')\n"
    )
    monkeypatch.setattr(commands, "__path__", [*commands.__path__, str(tmp_path)])
    # Restore the import cache as well as the package path after discovery.
    monkeypatch.setitem(sys.modules, "sherd_cli.commands.temporary_command", None)
    del sys.modules["sherd_cli.commands.temporary_command"]
    result = CliRunner().invoke(create_app(), ["temporary"])
    assert result.exit_code == 0
    assert result.output == "discovered command ran\n"


def test_no_args_overview() -> None:
    result = CliRunner().invoke(create_app(), [])
    assert result.exit_code == 0
    for verb in ("dig", "ask", "show", "web", "demo"):
        assert verb in result.stdout


def test_every_registered_command_has_summary_and_example() -> None:
    from typer.core import TyperGroup
    from typer.main import get_command

    app = create_app()
    root = get_command(app)

    def walk(group: TyperGroup, prefix: list[str]) -> None:
        for name, command in group.commands.items():
            args = [*prefix, name]
            result = CliRunner().invoke(app, [*args, "--help"])
            assert result.exit_code == 0, result.output
            assert "Example:" in result.stdout, args
            assert command.help, args
            assert command.help.splitlines()[0].strip(), args
            if isinstance(command, TyperGroup):
                walk(command, args)

    assert isinstance(root, TyperGroup)
    walk(root, [])


@pytest.mark.parametrize("args", [["show"], ["ask", "synthetic?"], ["export", "--all"], ["tui"]])
def test_missing_db_is_one_line_with_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    result = CliRunner().invoke(create_app(), args)
    assert result.exit_code == 1
    assert result.stderr.splitlines() == ["No database yet. Run `sherd demo` or `sherd dig PATH`."]
    assert "Traceback" not in result.output
    assert not (tmp_path / "life.duckdb").exists()


def test_unknown_dig_and_bad_path_are_one_line_with_next_step(tmp_path: Path) -> None:
    from sherd_core import Store

    path = tmp_path / "db.duckdb"
    with Store.open(path):
        pass
    for args, hint in [
        (["show", "missing", "--db", str(path)], "show --list"),
        (["export", "--dig", "missing", "--db", str(path)], "show --list"),
        (["dig", str(tmp_path / "missing")], "check the export path"),
    ]:
        result = CliRunner().invoke(create_app(), args)
        assert result.exit_code == 1
        assert len(result.stderr.splitlines()) == 1
        assert hint in result.stderr
        assert "Traceback" not in result.output
