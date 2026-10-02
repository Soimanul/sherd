"""Adapters against fake transports: the real SDK request path, no network."""

import json
from typing import Any

import httpx
import httpx2
import pytest
from sherd_agent import providers
from sherd_agent.providers import ChatMessage, ProviderError, load_settings
from sherd_agent.providers import ollama as ollama_module
from sherd_agent.providers.anthropic_api import FALLBACK_BETA, AnthropicProvider
from sherd_agent.providers.ollama import OllamaProvider
from sherd_agent.providers.openai_api import OpenAIProvider
from sherd_agent.providers.stub import StubProvider

MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "You plan."},
    {"role": "user", "content": "How many messages?"},
]
SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
Seen = list[dict[str, Any]]


def anthropic_client(seen: Seen, status: int = 200, **overrides: Any) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
                "size": len(request.content),
            }
        )
        body = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": '{"sql": "SELECT 1"}'},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 15},
            **overrides,
        }
        if status != 200:
            body = {"type": "error", "error": {"type": "error", "message": "nope"}}
        return httpx2.Response(status, json=body)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_anthropic_request_shape_and_completion() -> None:
    seen: Seen = []
    provider = AnthropicProvider(
        "claude-test",
        api_key="test-key",
        max_tokens=512,
        options={"effort": "low", "fallbacks": "default"},
        http_client=anthropic_client(seen),
    )
    completion = provider.complete(MESSAGES, json_schema=SCHEMA)

    assert completion.text == '{"sql": "SELECT 1"}'
    assert (completion.input_tokens, completion.output_tokens) == (120, 15)
    assert completion.bytes_sent > 0
    assert completion.bytes_received > 0
    (request,) = seen
    assert request["url"].endswith("/v1/messages?beta=true")
    assert request["headers"]["x-api-key"] == "test-key"
    assert FALLBACK_BETA in request["headers"]["anthropic-beta"]
    body = request["body"]
    assert body["model"] == "claude-test"
    assert body["max_tokens"] == 512
    assert body["system"] == "You plan."
    assert body["messages"] == [{"role": "user", "content": "How many messages?"}]
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert body["fallbacks"] == "default"
    assert completion.bytes_sent == request["size"]


def test_anthropic_without_options_or_schema_sends_neither() -> None:
    seen: Seen = []
    provider = AnthropicProvider("other", api_key="k", http_client=anthropic_client(seen))
    provider.complete(MESSAGES, json_schema=None)
    body = seen[0]["body"]
    assert "output_config" not in body
    assert "fallbacks" not in body
    assert "anthropic-beta" not in seen[0]["headers"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [({"stop_reason": "refusal"}, "declined"), ({"stop_reason": "max_tokens"}, "output tokens")],
)
def test_anthropic_stop_reasons(overrides: dict[str, Any], message: str) -> None:
    provider = AnthropicProvider("m", api_key="k", http_client=anthropic_client([], **overrides))
    with pytest.raises(ProviderError, match=message):
        provider.complete(MESSAGES, json_schema=None)


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the API key"), (404, "no model"), (400, "HTTP 400")],
)
def test_anthropic_errors_are_safe_messages(status: int, message: str) -> None:
    provider = AnthropicProvider(
        "m", api_key="secret-key", http_client=anthropic_client([], status=status)
    )
    with pytest.raises(ProviderError, match=message) as caught:
        provider.complete(MESSAGES, json_schema=None)
    assert "secret-key" not in str(caught.value)


def openai_client(seen: Seen, status: int = 200, refusal: str | None = None) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "nope", "type": "x"}})
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"sql": "SELECT 1"}',
                            "refusal": refusal,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 90, "completion_tokens": 9, "total_tokens": 99},
            },
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_openai_request_shape_and_completion() -> None:
    seen: Seen = []
    provider = OpenAIProvider("gpt-test", api_key="test-key", http_client=openai_client(seen))
    completion = provider.complete(MESSAGES, json_schema=SCHEMA)

    assert completion.text == '{"sql": "SELECT 1"}'
    assert (completion.input_tokens, completion.output_tokens) == (90, 9)
    assert completion.bytes_sent > 0
    assert completion.bytes_received > 0
    body = seen[0]["body"]
    assert seen[0]["headers"]["authorization"] == "Bearer test-key"
    assert body["model"] == "gpt-test"
    assert body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"}

    provider.complete(MESSAGES, json_schema=None)
    assert "response_format" not in seen[1]["body"]


def test_openai_refusal_and_errors() -> None:
    refusing = OpenAIProvider("m", api_key="k", http_client=openai_client([], refusal="no"))
    with pytest.raises(ProviderError, match="declined"):
        refusing.complete(MESSAGES, json_schema=None)
    failing = OpenAIProvider("m", api_key="secret", http_client=openai_client([], status=401))
    with pytest.raises(ProviderError, match="rejected the API key") as caught:
        failing.complete(MESSAGES, json_schema=None)
    assert "secret" not in str(caught.value)


def ollama_transport(seen: Seen, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": request.content})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if status != 200:
            return httpx.Response(status, json={"error": "model not found"})
        return httpx.Response(
            200,
            json={
                "model": "qwen-test",
                "message": {"role": "assistant", "content": '{"sql": "SELECT 1"}'},
                "done": True,
                "prompt_eval_count": 70,
                "eval_count": 7,
            },
        )

    return httpx.MockTransport(handler)


def test_ollama_request_shape_and_completion() -> None:
    seen: Seen = []
    provider = OllamaProvider("qwen-test", transport=ollama_transport(seen))
    completion = provider.complete(MESSAGES, json_schema=SCHEMA)

    assert completion.text == '{"sql": "SELECT 1"}'
    assert (completion.input_tokens, completion.output_tokens) == (70, 7)
    assert seen[0]["url"] == "http://127.0.0.1:11434/api/chat"
    body = json.loads(seen[0]["body"])
    assert body == {
        "model": "qwen-test",
        "messages": MESSAGES,
        "stream": False,
        "options": {"temperature": 0},
        "format": SCHEMA,
    }
    assert completion.bytes_sent == len(seen[0]["body"])
    assert not provider.remote


def test_ollama_missing_model_and_unreachable() -> None:
    missing = OllamaProvider("absent", transport=ollama_transport([], status=404))
    with pytest.raises(ProviderError, match="ollama pull absent"):
        missing.complete(MESSAGES, json_schema=None)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    down = OllamaProvider("m", transport=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError, match="not reachable"):
        down.complete(MESSAGES, json_schema=None)
    assert not ollama_module.available(transport=httpx.MockTransport(refuse))
    assert ollama_module.available(transport=ollama_transport([]))


@pytest.mark.parametrize(
    "host", ["http://192.0.2.1:11434", "https://127.0.0.1:11434", "http://example.com"]
)
def test_ollama_host_must_be_loopback(host: str) -> None:
    with pytest.raises(ProviderError, match="loopback"):
        OllamaProvider("m", host=host)
    assert not ollama_module.available(host)


def test_stub_plans_replies_and_answers() -> None:
    stub = StubProvider({"How many messages?": {"sql": "SELECT 1"}}, replies=["not json"])
    assert stub.complete(MESSAGES, json_schema=SCHEMA).text == "not json"
    assert json.loads(stub.complete(MESSAGES, json_schema=SCHEMA).text) == {"sql": "SELECT 1"}
    assert stub.complete(MESSAGES, json_schema=None).text.startswith("Stub answer")
    other: list[ChatMessage] = [{"role": "user", "content": "unknown"}]
    assert "refuse" in json.loads(stub.complete(other, json_schema=SCHEMA).text)
    assert len(stub.calls) == 4


def test_stub_from_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "plans.yaml"
    script.write_text('How many messages?: {"dig": "messages.volume_by_contact"}\n')
    monkeypatch.setenv("SHERD_AGENT_STUB", str(script))
    stub = StubProvider.from_env()
    assert json.loads(stub.complete(MESSAGES, json_schema=SCHEMA).text) == {
        "dig": "messages.volume_by_contact"
    }


def test_shipped_settings() -> None:
    settings = load_settings()
    assert settings.default_order == ("ollama", "anthropic", "openai")
    assert settings.default_model("ollama") == "qwen2.5-coder:7b"
    assert settings.default_model("anthropic")
    assert settings.default_model("openai") is None


def test_settings_reject_unknown_providers() -> None:
    with pytest.raises(ValueError, match="unknown providers"):
        load_settings('default_order = ["gemini"]')


def test_create_reads_keys_from_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not providers.available("anthropic", settings)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        providers.create("anthropic", "m", settings)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert providers.available("anthropic", settings)
    assert providers.available("openai", settings)
    default = providers.create("anthropic", str(settings.default_model("anthropic")), settings)
    other = providers.create("anthropic", "some-other-model", settings)
    assert isinstance(default, AnthropicProvider)
    assert isinstance(other, AnthropicProvider)
    assert default.options == dict(settings.get("anthropic")["default_model_options"])
    assert other.options == {}
    assert isinstance(providers.create("openai", "gpt-test", settings), OpenAIProvider)
    assert isinstance(providers.create("stub", "canned", settings), StubProvider)
    assert providers.create("ollama", "q", settings).remote is False
    with pytest.raises(ProviderError, match="unknown provider"):
        providers.create("gemini", "m", settings)
