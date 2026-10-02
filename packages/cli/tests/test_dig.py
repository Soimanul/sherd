import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sherd_cli import config
from sherd_cli.commands import dig
from sherd_cli.main import create_app
from sherd_connectors import registry
from sherd_connectors.base import Connector, DetectResult, ImportContext
from sherd_core import Event, Row, Store
from typer.testing import CliRunner, Result


class FakeConnector:
    """Parses `local-ts|title` lines; records the context of its last parse."""

    version = "1"

    def __init__(self, connector_id: str, confidence: float, fail: bool = False) -> None:
        self.id = connector_id
        self.display_name = connector_id.title()
        self.confidence = confidence
        self.fail = fail
        self.contexts: list[ImportContext] = []

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(self.confidence, f"{self.id} markers")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        self.contexts.append(ctx)
        if self.fail:
            raise ValueError("line 2 is not a timestamp")
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines):
                stamp, title = line.rstrip("\n").split("|", 1)
                yield Event(
                    source_file=path.name,
                    source_row_id=f"{number}",
                    ts=datetime.fromisoformat(stamp).replace(tzinfo=ctx.tz),
                    kind="shell.command",
                    title=title,
                )

    def fixtures(self) -> list[Path]:
        return []


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("SHERD_HOME", str(path))
    monkeypatch.setattr(dig, "local_zone", lambda: "America/Chicago")
    return path


@pytest.fixture
def export(tmp_path: Path) -> Path:
    path = tmp_path / "history.txt"
    path.write_text(
        "2024-03-01T09:00:00|git status\n2024-03-01T09:01:00|ls\n2024-03-02T10:00:00|uv sync\n"
    )
    return path


def install(monkeypatch: pytest.MonkeyPatch, *connectors: FakeConnector) -> None:
    found: dict[str, Connector] = {c.id: c for c in connectors}
    monkeypatch.setattr(registry, "discover", lambda: found)


def run(*args: str, stdin: str | None = None) -> Result:
    return CliRunner().invoke(create_app(), ["dig", *args], input=stdin)


def one_line_error(result: Result) -> str:
    assert "Traceback" not in result.output
    assert result.stderr.startswith("error: ")
    return result.stderr


def events(home: Path) -> int:
    with Store.open(home / "life.duckdb", read_only=True) as store:
        return store.table_counts()["events"]


def test_success_picks_the_clear_winner(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = FakeConnector("alpha", 0.9), FakeConnector("beta", 0.3)
    install(monkeypatch, alpha, beta)
    result = run(str(export))
    assert result.exit_code == 0, result.output
    assert "Detected Alpha (90%: alpha markers)" in result.stdout
    assert "Imported with Alpha: 3 rows seen, 3 new" in result.stdout
    assert "Time zone: America/Chicago (from the system time zone; override with --tz" in (
        result.stdout
    )
    assert len(alpha.contexts) == 1
    assert not beta.contexts
    assert alpha.contexts[0].export_root == export.parent
    assert events(home) == 3

    again = run(str(export))
    assert "3 rows seen, 0 new" in again.stdout


@pytest.mark.parametrize(
    ("confidences", "listed"),
    [
        pytest.param((0.4, 0.1), ["alpha"], id="low-confidence"),
        pytest.param((0.9, 0.85), ["alpha", "beta"], id="tie"),
        pytest.param((0.6, 0.5), ["alpha", "beta"], id="tie-at-exactly-0.1"),
    ],
)
def test_unsure_detection_exits_2_when_not_interactive(
    home: Path,
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    confidences: tuple[float, float],
    listed: list[str],
) -> None:
    alpha, beta = FakeConnector("alpha", confidences[0]), FakeConnector("beta", confidences[1])
    install(monkeypatch, alpha, beta)
    result = run(str(export))
    assert result.exit_code == 2
    message = one_line_error(result)
    assert "cannot tell which connector reads" in message
    for connector_id in listed:
        assert f"{connector_id} " in message
    assert "--connector ID" in message
    assert not alpha.contexts
    assert not beta.contexts
    assert not (home / "life.duckdb").exists()


def test_yes_never_prompts(home: Path, export: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9), FakeConnector("beta", 0.85))
    monkeypatch.setattr(dig, "interactive", lambda: True)
    assert run(str(export), "--yes").exit_code == 2


def test_interactive_prompt_offers_a_choice(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = FakeConnector("alpha", 0.9), FakeConnector("beta", 0.85)
    install(monkeypatch, alpha, beta)
    monkeypatch.setattr(dig, "interactive", lambda: True)
    result = run(str(export), stdin="7\n2\n")
    assert result.exit_code == 0, result.output
    assert "1. alpha" in result.stdout
    assert "2. beta" in result.stdout
    assert "Pick a number from 1 to 2." in result.stdout
    assert beta.contexts
    assert not alpha.contexts


def test_connector_flag_overrides_detection(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = FakeConnector("alpha", 0.9), FakeConnector("beta", 0.0)
    install(monkeypatch, alpha, beta)
    result = run(str(export), "--connector", "beta")
    assert result.exit_code == 0, result.output
    assert "Imported with Beta" in result.stdout
    assert beta.contexts
    assert not alpha.contexts


def test_unknown_connector_flag(home: Path, export: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9))
    result = run(str(export), "--connector", "nope")
    assert result.exit_code == 2
    assert "unknown connector 'nope'; installed: alpha" in one_line_error(result)


def test_tz_precedence(home: Path, export: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = FakeConnector("alpha", 0.9)
    other = FakeConnector("other", 0.0)
    install(monkeypatch, alpha, other)

    first = run(str(export))
    assert "Time zone: America/Chicago (from the system time zone" in first.stdout
    flagged = run(str(export), "--tz", "Asia/Tokyo")
    assert "Time zone: Asia/Tokyo (from --tz" in flagged.stdout
    remembered = run(str(export))
    assert "Time zone: Asia/Tokyo (from the previous alpha import" in remembered.stdout
    flag_wins = run(str(export), "--tz", "Europe/Bucharest")
    assert "Time zone: Europe/Bucharest (from --tz" in flag_wins.stdout
    assert [ctx.tz.key for ctx in alpha.contexts] == [
        "America/Chicago",
        "Asia/Tokyo",
        "Asia/Tokyo",
        "Europe/Bucharest",
    ]
    # Another connector's history does not leak into this one.
    fresh = run(str(export), "--connector", "other")
    assert "Time zone: America/Chicago (from the system time zone" in fresh.stdout
    with Store.open(home / "life.duckdb", read_only=True) as store:
        zones = store.query("SELECT tz FROM imports ORDER BY started_at").column("tz")
    assert zones.to_pylist()[1] == "Asia/Tokyo"


def test_failed_import_does_not_set_the_zone(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = FakeConnector("alpha", 0.9, fail=True)
    install(monkeypatch, broken)
    assert run(str(export), "--tz", "Asia/Tokyo").exit_code == 1
    install(monkeypatch, FakeConnector("alpha", 0.9))
    assert "Time zone: America/Chicago" in run(str(export)).stdout


def test_invalid_tz(home: Path, export: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9))
    result = run(str(export), "--tz", "Mars/Olympus")
    assert result.exit_code == 2
    assert "unknown time zone 'Mars/Olympus'" in one_line_error(result)


def test_me_is_persisted_per_connector_and_reused(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, other = FakeConnector("alpha", 0.9), FakeConnector("other", 0.0)
    install(monkeypatch, alpha, other)
    result = run(str(export), "--me", "Alex Demo", "--me", "+1 555 0100")
    assert result.exit_code == 0, result.output
    assert "Treating 2 identities as you" in result.stdout
    saved = json.loads((home / "config.json").read_text())
    assert saved == {"connectors": {"alpha": {"me": ["Alex Demo", "+1 555 0100"]}}}

    assert run(str(export)).exit_code == 0
    assert [ctx.self_identities for ctx in alpha.contexts] == [
        frozenset({"Alex Demo", "+1 555 0100"})
    ] * 2

    run(str(export), "--connector", "other")
    assert other.contexts[0].self_identities == frozenset()

    run(str(export), "--me", "Alex")
    assert config.self_identities("alpha") == ["Alex"]
    assert alpha.contexts[-1].self_identities == frozenset({"Alex"})


def test_missing_path(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9))
    result = run(str(tmp_path / "nowhere"))
    assert result.exit_code == 1
    assert "no such file or folder" in one_line_error(result)


def test_parse_error_is_one_line_and_marks_the_import_failed(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9, fail=True))
    result = run(str(export))
    assert result.exit_code == 1
    message = one_line_error(result)
    assert "alpha could not read" in message
    assert "ValueError: line 2 is not a timestamp" in message
    assert len(message.strip().splitlines()) == 1
    with Store.open(home / "life.duckdb", read_only=True) as store:
        status = store.query("SELECT status FROM imports").column("status").to_pylist()
    assert status == ["failed"]


def test_debug_env_shows_the_traceback(
    home: Path, export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9, fail=True))
    monkeypatch.setenv(dig.DEBUG_ENV, "1")
    result = run(str(export))
    assert isinstance(result.exception, ValueError)


def test_no_connectors(home: Path, export: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    result = run(str(export))
    assert result.exit_code == 1
    assert "no connectors are installed" in one_line_error(result)


def test_local_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert dig.local_zone() == "Pacific/Auckland"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert dig.local_zone() != "Not/AZone"


def test_ambiguity_rule() -> None:
    def ranked(*confidences: float) -> list[tuple[Connector, DetectResult]]:
        return [(FakeConnector(f"c{i}", c), DetectResult(c, "")) for i, c in enumerate(confidences)]

    assert not dig.ambiguous(ranked(0.5))
    assert dig.ambiguous(ranked(0.49))
    assert not dig.ambiguous(ranked(0.9, 0.79))
    assert dig.ambiguous(ranked(0.9, 0.8))
    assert dig.ambiguous(ranked())


@pytest.mark.parametrize("me_args", [[], ["--me", "Alex Demo"]], ids=["read", "write"])
@pytest.mark.parametrize(
    "data",
    [
        [],
        {"connectors": []},
        {"connectors": {"alpha": []}},
        {"connectors": {"alpha": {"me": "Alex Demo"}}},
        {"connectors": {"alpha": {"me": [123]}}},
        {"connectors": {"other": {"me": None}}},
    ],
)
def test_invalid_config_shape_is_one_line_error(
    home: Path,
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    me_args: list[str],
    data: object,
) -> None:
    install(monkeypatch, FakeConnector("alpha", 0.9))
    home.mkdir()
    path = home / "config.json"
    baseline = json.dumps(data)
    path.write_text(baseline)
    with pytest.raises(ValueError, match=r"must (hold|be)"):
        config.load()
    result = run(str(export), *me_args)
    assert result.exit_code == 1
    assert len(one_line_error(result).strip().splitlines()) == 1
    assert path.read_text() == baseline
