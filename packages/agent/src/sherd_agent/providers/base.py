"""The provider contract shared by every LLM adapter."""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal, Protocol, TypedDict

REMOTE = frozenset({"anthropic", "openai"})
LOCAL = frozenset({"ollama", "stub"})
NAMES = REMOTE | LOCAL


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    bytes_sent: int
    bytes_received: int
    requests: int = 1


class Provider(Protocol):
    name: str
    model: str
    remote: bool

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion: ...


class ProviderError(Exception):
    """A provider call failed; the message is safe to show (no keys, no data)."""


@dataclass(frozen=True)
class Settings:
    """The shipped defaults from `providers.toml`."""

    default_order: tuple[str, ...]
    providers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def get(self, provider: str) -> Mapping[str, Any]:
        return self.providers.get(provider, {})

    def default_model(self, provider: str) -> str | None:
        model = self.get(provider).get("model")
        return str(model) if model else None


def load_settings(text: str | None = None) -> Settings:
    """Parse `text`, or the `providers.toml` shipped with sherd_agent."""
    if text is None:
        text = files("sherd_agent").joinpath("providers.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    order = tuple(str(name) for name in data.pop("default_order", ()))
    unknown = [name for name in (*order, *data) if name not in NAMES]
    if unknown:
        raise ValueError(f"providers.toml names unknown providers: {unknown}")
    return Settings(order, {name: dict(value) for name, value in data.items()})


def body_size(messages: Sequence[ChatMessage]) -> int:
    """UTF-8 size of the message texts; used when no request body is available."""
    return sum(len(message["content"].encode()) for message in messages)
