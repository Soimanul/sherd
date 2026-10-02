"""LLM providers: the only place in sherd allowed to open network connections."""

import os

from sherd_agent.providers.base import (
    LOCAL,
    NAMES,
    REMOTE,
    ChatMessage,
    Completion,
    Provider,
    ProviderError,
    Settings,
    load_settings,
)

__all__ = [
    "LOCAL",
    "NAMES",
    "REMOTE",
    "ChatMessage",
    "Completion",
    "Provider",
    "ProviderError",
    "Settings",
    "available",
    "create",
    "load_settings",
]


def _key_env(settings: Settings, name: str) -> str:
    return str(settings.get(name).get("api_key_env", f"{name.upper()}_API_KEY"))


def available(name: str, settings: Settings) -> bool:
    """Ollama: answers on loopback. Remote: its API key variable is set. Stub: always."""
    if name == "ollama":
        from sherd_agent.providers import ollama

        return ollama.available(str(settings.get(name).get("host", ollama.DEFAULT_HOST)))
    if name in REMOTE:
        return bool(os.environ.get(_key_env(settings, name)))
    return name == "stub"


def create(name: str, model: str, settings: Settings) -> Provider:
    """Build the provider `name` for `model`; API keys come from the environment only."""
    config = settings.get(name)
    if name == "stub":
        from sherd_agent.providers.stub import StubProvider

        return StubProvider.from_env(model)
    if name == "ollama":
        from sherd_agent.providers.ollama import DEFAULT_HOST, OllamaProvider

        return OllamaProvider(model, host=str(config.get("host", DEFAULT_HOST)))
    if name in REMOTE:
        key = os.environ.get(_key_env(settings, name))
        if not key:
            raise ProviderError(f"{name} needs ${_key_env(settings, name)} to be set")
        if name == "anthropic":
            from sherd_agent.providers.anthropic_api import AnthropicProvider

            options = config.get("default_model_options", {})
            return AnthropicProvider(
                model,
                api_key=key,
                max_tokens=int(config.get("max_tokens", 8192)),
                # Options are tuned for the shipped model; other models may reject them.
                options=options if model == settings.default_model(name) else None,
            )
        from sherd_agent.providers.openai_api import OpenAIProvider

        return OpenAIProvider(model, api_key=key)
    raise ProviderError(f"unknown provider {name!r}; choose one of {', '.join(sorted(NAMES))}")
