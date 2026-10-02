"""Ollama on loopback: local models, nothing leaves the machine."""

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from sherd_agent.providers.base import ChatMessage, Completion, ProviderError

DEFAULT_HOST = "http://127.0.0.1:11434"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PROBE_TIMEOUT_S = 0.5
CALL_TIMEOUT_S = 300.0


def require_loopback(host: str) -> str:
    """`host` without a trailing slash; refuses anything but a loopback http URL."""
    parts = urlsplit(host)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ProviderError(f"ollama host must be http on loopback, got {host!r}")
    return host.rstrip("/")


def available(host: str = DEFAULT_HOST, *, transport: httpx.BaseTransport | None = None) -> bool:
    """True when Ollama answers `/api/tags` on loopback."""
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, transport=transport) as client:
            return client.get(require_loopback(host) + "/api/tags").is_success
    except (httpx.HTTPError, ProviderError, OSError):
        return False


class OllamaProvider:
    name = "ollama"
    remote = False

    def __init__(
        self,
        model: str,
        *,
        host: str = DEFAULT_HOST,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.host = require_loopback(host)
        self._transport = transport

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": 0},
        }
        if json_schema is not None:
            body["format"] = dict(json_schema)
        payload = json.dumps(body).encode()
        try:
            with httpx.Client(timeout=CALL_TIMEOUT_S, transport=self._transport) as client:
                response = client.post(
                    self.host + "/api/chat",
                    content=payload,
                    headers={"content-type": "application/json"},
                )
        except httpx.HTTPError as error:
            raise ProviderError(f"ollama is not reachable at {self.host}: {error}") from None
        if response.status_code == 404:
            raise ProviderError(
                f"ollama has no model {self.model!r}; run `ollama pull {self.model}`"
            )
        if not response.is_success:
            raise ProviderError(f"ollama returned HTTP {response.status_code}")
        try:
            data = response.json()
            text = str(data["message"]["content"])
        except (ValueError, KeyError, TypeError):
            raise ProviderError("ollama returned an unexpected response") from None
        return Completion(
            text=text,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            bytes_sent=len(payload),
            bytes_received=len(response.content),
        )
