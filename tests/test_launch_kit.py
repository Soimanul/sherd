"""Launch copy stays aligned with the shipped CLI and synthetic Wrapped output."""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from sherd_cli.main import app
from sherd_core import Store
from sherd_insights import wrapped
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "docs" / "launch"
RUNNER = CliRunner()


def commands(text: str) -> list[list[str]]:
    """Find actual sherd invocations in inline code and shell code fences."""
    snippets = re.findall(r"`(sherd [^`\n]+)`", text)
    for block in re.findall(r"```sh\n(.*?)```", text, re.S):
        snippets.extend(line for line in block.splitlines() if line.startswith("sherd "))
    return [
        shlex.split(segment)
        for part in snippets
        for segment in part.split(" && ")
        if segment.startswith("sherd ")
    ]


def command_options(name: str) -> set[str]:
    """Option names of a CLI command, read from Click rather than from rendered help."""
    group = typer.main.get_command(app)
    assert isinstance(group, typer.core.TyperGroup)
    command = group.commands[name]
    return {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}


def test_commands_and_flags_are_in_real_help() -> None:
    checked = 0
    for doc in LAUNCH.glob("*.md"):
        for tokens in commands(doc.read_text()):
            assert tokens[0] == "sherd"
            command = tokens[1]
            result = RUNNER.invoke(app, [command, "--help"])
            assert result.exit_code == 0, f"{doc.name}: {command}: {result.output}"
            options = command_options(command)
            for flag in (part.split("=")[0] for part in tokens[2:] if part.startswith("--")):
                assert flag in options, f"{doc.name}: {flag} is not an option of {command}"
            checked += 1
    assert checked >= 15


def test_launch_image_links_resolve() -> None:
    checked = 0
    for doc in LAUNCH.glob("*.md"):
        for target in re.findall(r"!?\[[^]]*\]\((images/[^)]+)\)", doc.read_text()):
            assert (doc.parent / target).is_file(), f"{doc.name}: {target}"
            checked += 1
    assert checked >= 4
    assert len(list((LAUNCH / "images").glob("wrapped-*.png"))) == 6
    assert sum(path.stat().st_size for path in (LAUNCH / "images").glob("*.png")) < 3_000_000


def test_show_hn_limits() -> None:
    text = (LAUNCH / "show-hn.md").read_text()
    title = text.splitlines()[0].removeprefix("# ")
    comment = text.split("## First comment\n", 1)[1]
    assert title.startswith("Show HN: sherd ")
    assert len(title) <= 80
    assert len(comment.split()) <= 350


def test_launch_folder_passes_pii_scan() -> None:
    files = sorted(str(path) for path in LAUNCH.rglob("*") if path.is_file())
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pii_scan.py"), *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_wrapped_share_flow_on_demo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI uses SHERD_HOME for the demo database. Keep this test isolated.
    monkeypatch.setenv("SHERD_HOME", str(tmp_path / "home"))
    built = RUNNER.invoke(app, ["demo"])
    assert built.exit_code == 0, built.output
    out = tmp_path / "cards"
    made = RUNNER.invoke(app, ["wrapped", "--demo", "--out", str(out)])
    assert made.exit_code == 0, made.output
    pngs = sorted(out.glob("wrapped-*.png"))
    assert len(pngs) == 6
    assert all(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in pngs)

    with Store.open(tmp_path / "home" / "demo.duckdb", read_only=True) as store:
        year = wrapped.default_year(store, "UTC")
        assert year is not None
        cards = wrapped.build(store, year, wrapped.WrappedOptions(tz="UTC"))
        specs = json.dumps([card.spec for card in cards])
        names = store.query(
            "SELECT DISTINCT chat_name AS name FROM messages WHERE chat_kind = 'direct' "
            "UNION SELECT display_name AS name FROM contacts"
        ).to_pylist()
        for row in names:
            name = row["name"]
            if isinstance(name, str) and " " in name:
                assert name not in specs
        assert not re.search(r"\d[\d,]*\.\d\d\s*(?:RON|USD|EUR)", specs)
        assert not any(code in specs for code in ("RON", "USD", "EUR"))
