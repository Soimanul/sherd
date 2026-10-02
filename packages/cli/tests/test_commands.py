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
