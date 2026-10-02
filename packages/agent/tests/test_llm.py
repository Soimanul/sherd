import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from sherd_agent import config, privacy
from sherd_agent.llm import LLM, NoProviderError, OfflineRefusedError, Probe, resolve
from sherd_agent.providers import ChatMessage, Completion, ProviderError, Settings, load_settings
from sherd_agent.providers.stub import StubProvider

SETTINGS = load_settings()


def probe_for(*up: str) -> Probe:
    return lambda name, settings: name in up


def test_cli_flags_win() -> None:
    choice = resolve(
        SETTINGS,
        provider="anthropic",
        model="flag-model",
        config={"provider": "ollama", "model": "cfg"},
        probe=probe_for("ollama", "anthropic"),
    )
    assert (choice.provider, choice.model, choice.remote) == ("anthropic", "flag-model", True)


def test_config_beats_defaults() -> None:
    choice = resolve(
        SETTINGS,
        config={"provider": "openai", "model": "cfg-model"},
        probe=probe_for("ollama", "openai"),
    )
    assert (choice.provider, choice.model) == ("openai", "cfg-model")


def test_config_model_only_applies_to_its_provider() -> None:
    choice = resolve(
        SETTINGS,
        provider="ollama",
        config={"provider": "openai", "model": "cfg-model"},
        probe=probe_for("ollama"),
    )
    assert (choice.provider, choice.model) == ("ollama", "qwen2.5-coder:7b")


@pytest.mark.parametrize(
    ("up", "expected"),
    [
        (("ollama", "anthropic", "openai"), "ollama"),
        (("anthropic", "openai"), "anthropic"),
        (("openai",), "openai"),
    ],
)
def test_default_order(up: tuple[str, ...], expected: str) -> None:
    if expected == "openai":
        with pytest.raises(NoProviderError, match="no default model"):
            resolve(SETTINGS, probe=probe_for(*up))
        assert resolve(SETTINGS, model="gpt-x", probe=probe_for(*up)).provider == "openai"
        return
    choice = resolve(SETTINGS, probe=probe_for(*up))
    assert choice.provider == expected
    assert choice.model == SETTINGS.default_model(expected)


def test_default_order_is_read_from_settings() -> None:
    settings = Settings(("anthropic", "ollama"), SETTINGS.providers)
    assert resolve(settings, probe=probe_for("ollama", "anthropic")).provider == "anthropic"


def test_nothing_available_explains_both_paths() -> None:
    with pytest.raises(NoProviderError) as caught:
        resolve(SETTINGS, probe=probe_for())
    message = str(caught.value)
    assert "ollama pull qwen2.5-coder:7b" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "OPENAI_API_KEY" in message
    assert "sherd show" in message


def test_named_but_unavailable_provider() -> None:
    with pytest.raises(NoProviderError, match="'anthropic' is not available"):
        resolve(SETTINGS, provider="anthropic", probe=probe_for("ollama"))
    with pytest.raises(NoProviderError, match="unknown provider"):
        resolve(SETTINGS, provider="gemini", probe=probe_for("gemini"))


def test_offline_refuses_named_remote_and_skips_remote_defaults() -> None:
    with pytest.raises(OfflineRefusedError):
        resolve(SETTINGS, provider="openai", offline=True, probe=probe_for("openai"))
    with pytest.raises(OfflineRefusedError):
        resolve(SETTINGS, config={"provider": "anthropic"}, offline=True, probe=probe_for())
    with pytest.raises(NoProviderError):
        resolve(SETTINGS, offline=True, probe=probe_for("anthropic", "openai"))
    assert resolve(SETTINGS, offline=True, probe=probe_for("ollama", "anthropic")).provider == (
        "ollama"
    )


def test_stub_is_local_with_a_canned_model() -> None:
    choice = resolve(SETTINGS, provider="stub", probe=probe_for("stub"))
    assert (choice.provider, choice.model, choice.remote) == ("stub", "canned", False)


class RemoteStub(StubProvider):
    name = "anthropic"
    remote = True

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        completion = super().complete(messages, json_schema=json_schema)
        return Completion(completion.text, 100, 10, completion.bytes_sent, 50)


def test_privacy_counts_remote_and_local_separately(home: Path) -> None:
    remote = LLM(RemoteStub())
    local = LLM(StubProvider())
    remote.complete([{"role": "user", "content": "abcd"}])
    remote.complete([{"role": "user", "content": "ef"}])
    local.complete([{"role": "user", "content": "xyz"}])

    data = json.loads((home / "privacy.json").read_text())
    assert data["remote"] == {
        "anthropic": {
            "requests": 2,
            "bytes_sent": 6,
            "bytes_received": 100,
            "input_tokens": 200,
            "output_tokens": 20,
        }
    }
    assert data["local"]["stub"]["requests"] == 1
    assert data["local"]["stub"]["bytes_sent"] == 3
    assert privacy.remote_bytes_sent() == 6
    assert (remote.usage.requests, remote.usage.input_tokens, remote.usage.bytes_sent) == (
        2,
        200,
        6,
    )


def test_privacy_file_never_holds_message_text(home: Path) -> None:
    LLM(RemoteStub()).complete([{"role": "user", "content": "a secret question"}])
    assert "secret" not in (home / "privacy.json").read_text()


def test_consent_is_recorded_per_provider_and_keeps_other_settings(home: Path) -> None:
    (home / "config.json").write_text(json.dumps({"connectors": {"whatsapp": {"me": ["Me"]}}}))
    assert not config.has_consent("anthropic")
    config.record_consent("anthropic")
    assert config.has_consent("anthropic")
    assert not config.has_consent("openai")
    data = json.loads((home / "config.json").read_text())
    assert data["connectors"] == {"whatsapp": {"me": ["Me"]}}
    assert set(data["ask"]["consent"]) == {"anthropic"}


def test_config_validation(home: Path) -> None:
    (home / "config.json").write_text(json.dumps({"ask": {"provider": 3}}))
    with pytest.raises(ValueError, match=r"ask\.provider"):
        config.load()
    (home / "config.json").write_text(json.dumps({"ask": []}))
    with pytest.raises(ValueError, match="config ask"):
        config.load()


class FailingRemote(RemoteStub):
    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        raise ProviderError("anthropic returned HTTP 500")


def test_failed_calls_still_count_as_sent(home: Path) -> None:
    llm = LLM(FailingRemote())
    with pytest.raises(ProviderError):
        llm.complete([{"role": "user", "content": "abcd"}])
    totals = json.loads((home / "privacy.json").read_text())["remote"]["anthropic"]
    assert (totals["requests"], totals["bytes_sent"], totals["bytes_received"]) == (1, 4, 0)
    assert llm.usage.requests == 1
