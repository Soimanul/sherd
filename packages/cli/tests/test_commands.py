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
