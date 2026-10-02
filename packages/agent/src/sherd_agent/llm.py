"""Provider-agnostic LLM client: choose a provider, call it, count what was sent."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sherd_agent import privacy, providers
from sherd_agent.providers import REMOTE, ChatMessage, Completion, Provider, Settings

logger = logging.getLogger("sherd.agent")

Probe = Callable[[str, Settings], bool]


class NoProviderError(Exception):
    """No usable provider; the message explains how to set one up."""


class OfflineRefusedError(Exception):
    """A remote provider was requested while `--offline`."""


def setup_message(settings: Settings) -> str:
    ollama_model = settings.default_model("ollama") or "a model"
    return (
        "No language model is available for `sherd ask`. Either:\n"
        f"  - run Ollama locally (https://ollama.com), then `ollama pull {ollama_model}`; "
        "nothing leaves your machine, or\n"
        "  - set ANTHROPIC_API_KEY or OPENAI_API_KEY (OpenAI also needs --model) to use a "
        "remote model; sherd asks before the first question is sent.\n"
        "`sherd show` needs neither."
    )


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str

    @property
    def remote(self) -> bool:
        return self.provider in REMOTE


def resolve(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    config: Mapping[str, Any] | None = None,
    offline: bool = False,
    probe: Probe = providers.available,
) -> Choice:
    """`--provider/--model`, then the config "ask" object, then the first available default."""
    config = config or {}
    name = provider or config.get("provider")
    if name is not None:
        name = str(name)
        if name not in providers.NAMES:
            choices = ", ".join(sorted(providers.NAMES))
            raise NoProviderError(f"unknown provider {name!r}; choose one of {choices}")
        if offline and name in REMOTE:
            raise OfflineRefusedError(f"--offline refuses the remote provider {name!r}")
        if not probe(name, settings):
            raise NoProviderError(f"provider {name!r} is not available.\n{setup_message(settings)}")
    else:
        candidates = [n for n in settings.default_order if not (offline and n in REMOTE)]
        name = next((n for n in candidates if probe(n, settings)), None)
        if name is None:
            raise NoProviderError(setup_message(settings))
    chosen_model = (
        model
        or (config.get("model") if config.get("provider") == name else None)
        or settings.default_model(name)
        or ("canned" if name == "stub" else None)
    )
    if not chosen_model:
        raise NoProviderError(
            f"provider {name!r} has no default model; pass --model or set ask.model in config"
        )
    return Choice(name, str(chosen_model))


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0


class LLM:
    """Wraps a provider: every call is added to privacy.json and to this question's usage."""

    def __init__(self, provider: Provider, *, privacy_file: Path | None = None) -> None:
        self.provider = provider
        self.privacy_file = privacy_file
        self.usage = Usage()

    @property
    def remote(self) -> bool:
        return self.provider.remote

    def complete(
        self, messages: Sequence[ChatMessage], json_schema: Mapping[str, Any] | None = None
    ) -> Completion:
        completion = self.provider.complete(messages, json_schema=json_schema)
        self.usage.requests += 1
        self.usage.input_tokens += completion.input_tokens
        self.usage.output_tokens += completion.output_tokens
        self.usage.bytes_sent += completion.bytes_sent
        self.usage.bytes_received += completion.bytes_received
        privacy.record(self.provider.name, self.provider.remote, completion, self.privacy_file)
        logger.info(
            "llm call: provider=%s sent=%d received=%d tokens_in=%d tokens_out=%d",
            self.provider.name,
            completion.bytes_sent,
            completion.bytes_received,
            completion.input_tokens,
            completion.output_tokens,
        )
        return completion
