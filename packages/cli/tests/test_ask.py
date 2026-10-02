import json
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pytest_socket import SocketConnectBlockedError
from sherd_agent import providers
from sherd_agent.providers import ChatMessage, Completion, Provider, Settings
from sherd_agent.providers.netguard import OfflineError
from sherd_agent.providers.stub import StubProvider
from sherd_cli.commands.demo import build
from sherd_cli.main import create_app
from typer.testing import CliRunner, Result

QUESTION = "How many messages per month in 2025?"
MONTHLY = (
    "SELECT date_trunc('month', ts AT TIME ZONE 'Europe/Bucharest')::DATE AS month,"
    " count(*) AS messages FROM messages"
    " WHERE year(ts AT TIME ZONE 'Europe/Bucharest') = 2025 GROUP BY 1 ORDER BY 1"
)
PLANS = {
    QUESTION: {"sql": MONTHLY},
    "Who do I message most?": {"dig": "messages.volume_by_contact"},
}


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo.duckdb"
    build(path)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo_db: Path) -> Path:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Europe/Bucharest")
    (tmp_path / "demo.duckdb").symlink_to(demo_db)
    script = tmp_path / "plans.json"
    script.write_text(json.dumps(PLANS))
    monkeypatch.setenv("SHERD_AGENT_STUB", str(script))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def run(*args: str) -> Result:
    return CliRunner().invoke(create_app(), ["ask", *args])


class FakeRemote(StubProvider):
    """Stands in for a remote provider; counts calls instead of sending anything."""

    name = "anthropic"
    remote = True

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        completion = super().complete(messages, json_schema=json_schema)
        return Completion(completion.text, 1000, 40, completion.bytes_sent, 200)


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> list[FakeRemote]:
    created: list[FakeRemote] = []

    def create(name: str, model: str, settings: Settings) -> Provider:
        assert name == "anthropic"
        created.append(FakeRemote(PLANS, model=model))
        return created[-1]

    monkeypatch.setattr(providers, "available", lambda name, settings: name == "anthropic")
    monkeypatch.setattr(providers, "create", create)
    return created


def test_local_stub_shows_sql_first_then_rows_answer_table(home: Path) -> None:
    got = run(QUESTION, "--provider", "stub", "--demo")
    assert got.exit_code == 0, got.output
    lines = got.stdout.splitlines()
    assert lines[0] == f"SQL: {MONTHLY}"
    assert lines[1] == "Rows: 12"
    assert lines[2].startswith("Stub answer")
    assert "2025-03-01" in got.stdout
    assert "A chart is available" in got.stdout
    assert "Tokens:" not in got.stdout  # local providers have no token accounting
    assert "about to send" not in got.output  # and need no consent


def test_dig_plan_shows_dig_id_and_params(home: Path) -> None:
    got = run("Who do I message most?", "--provider", "stub", "--demo")
    assert got.exit_code == 0, got.output
    first = got.stdout.splitlines()[0]
    assert first.startswith("Dig: messages.volume_by_contact {")
    assert '"tz": "Europe/Bucharest"' in first


def test_json_includes_everything_and_the_chart_spec(home: Path) -> None:
    got = run(QUESTION, "--provider", "stub", "--db", str(home / "demo.duckdb"), "--json")
    assert got.exit_code == 0, got.output
    data = json.loads(got.stdout)
    assert data["sql"] == MONTHLY
    assert data["kind"] == "sql"
    assert data["columns"] == ["month", "messages"]
    assert data["row_count"] == 12
    assert len(data["rows"]) == 12
    assert data["chart"]["mark"] == "line"
    assert data["chart"]["$schema"].endswith("vega-lite/v5.json")
    assert data["provider"] == "stub"


def test_refusal(home: Path) -> None:
    got = run("What is the weather?", "--provider", "stub", "--demo")
    assert got.exit_code == 0, got.output
    assert got.stdout.startswith("Not answerable:")


def test_no_provider_explains_both_paths(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "available", lambda name, settings: False)
    got = run(QUESTION, "--demo")
    assert got.exit_code == 1
    assert "ollama pull qwen2.5-coder:7b" in got.stderr
    assert "ANTHROPIC_API_KEY" in got.stderr


def test_missing_database(home: Path) -> None:
    got = run(QUESTION, "--provider", "stub", "--db", str(home / "absent.duckdb"))
    assert got.exit_code == 1
    assert "No database yet" in got.stderr


def test_remote_requires_consent_and_sends_nothing_without_it(
    home: Path, remote: list[FakeRemote]
) -> None:
    got = run(QUESTION, "--demo")
    assert got.exit_code == 1
    assert "about to send data to anthropic" in got.stderr
    assert "### messages (" in got.stderr  # the catalog exactly as it would be sent
    assert "messages.volume_by_contact" in got.stderr  # the list of digs
    assert f"Your question: {QUESTION}" in got.stderr
    assert "up to 50 result rows" in got.stderr
    assert "nothing was sent; pass --yes" in got.stderr
    assert all(not provider.calls for provider in remote)
    assert not (home / "privacy.json").exists()
    assert not (home / "config.json").exists()


def test_remote_with_yes_records_consent_tokens_and_privacy(
    home: Path, remote: list[FakeRemote]
) -> None:
    got = run(QUESTION, "--demo", "--yes")
    assert got.exit_code == 0, got.output
    assert got.stdout.startswith(f"SQL: {MONTHLY}")
    assert "Tokens: 2000 in, 80 out over 2 requests to anthropic" in got.stdout
    config = json.loads((home / "config.json").read_text())
    assert "anthropic" in config["ask"]["consent"]
    counter = json.loads((home / "privacy.json").read_text())
    assert counter["remote"]["anthropic"]["requests"] == 2
    assert counter["remote"]["anthropic"]["bytes_received"] == 400
    assert counter["local"] == {}

    again = run(QUESTION, "--demo")  # consent is remembered
    assert again.exit_code == 0, again.output
    assert "about to send" not in again.output
    assert json.loads((home / "privacy.json").read_text())["remote"]["anthropic"]["requests"] == 4


def test_remote_json_carries_usage(home: Path, remote: list[FakeRemote]) -> None:
    got = run(QUESTION, "--demo", "--yes", "--json")
    assert got.exit_code == 0, got.output
    usage = json.loads(got.stdout)["usage"]
    assert (usage["requests"], usage["input_tokens"], usage["output_tokens"]) == (2, 2000, 80)


def test_offline_refuses_remote_before_any_call(home: Path, remote: list[FakeRemote]) -> None:
    got = run(QUESTION, "--demo", "--provider", "anthropic", "--offline", "--yes")
    assert got.exit_code == 1
    assert "--offline refuses the remote provider 'anthropic'" in got.stderr
    assert remote == []
    assert not (home / "privacy.json").exists()


class ConnectProbe(StubProvider):
    """A local provider that tries a public and a loopback connect while the command runs."""

    def __init__(self) -> None:
        super().__init__(PLANS)
        self.outcomes: dict[str, str] = {}

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        if not self.outcomes:
            with socket.socket() as client:
                try:
                    client.connect(("192.0.2.1", 443))
                    self.outcomes["public"] = "connected"
                except (OfflineError, SocketConnectBlockedError) as error:
                    self.outcomes["public"] = type(error).__name__
            with socket.socket() as listener, socket.socket() as client:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                client.connect(listener.getsockname())
                listener.accept()[0].close()
                self.outcomes["loopback"] = "connected"
        return super().complete(messages, json_schema=json_schema)


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.parametrize(("offline", "blocked_by"), [(True, "OfflineError"), (False, None)])
def test_offline_guard_covers_the_whole_command(
    home: Path, monkeypatch: pytest.MonkeyPatch, offline: bool, blocked_by: str | None
) -> None:
    probe = ConnectProbe()
    monkeypatch.setattr(providers, "create", lambda name, model, settings: probe)
    original = socket.socket.connect
    got = run(QUESTION, "--provider", "stub", "--demo", *(["--offline"] if offline else []))
    assert got.exit_code == 0, got.output
    assert probe.outcomes["loopback"] == "connected"
    # Without --offline only the test suite's own socket blocker stops the public connect.
    assert probe.outcomes["public"] == (blocked_by or "SocketConnectBlockedError")
    assert socket.socket.connect is original  # the guard is removed after the command


def test_failed_query_shows_the_sql_then_the_error(home: Path, tmp_path: Path) -> None:
    script = tmp_path / "bad.json"
    script.write_text(json.dumps({QUESTION: {"sql": "SELECT nope FROM messages"}}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SHERD_AGENT_STUB", str(script))
        got = run(QUESTION, "--provider", "stub", "--demo")
    assert got.exit_code == 1
    assert got.stdout.startswith("SQL: SELECT nope FROM messages")
    assert "error: the query failed" in got.stderr
