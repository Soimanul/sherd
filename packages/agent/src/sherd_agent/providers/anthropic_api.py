"""Claude through the official Anthropic SDK. Remote: needs consent and `ANTHROPIC_API_KEY`."""

import os
from collections.abc import Mapping, Sequence
from typing import Any

import anthropic
import httpx2
from anthropic.types.beta import BetaMessageParam

from sherd_agent.providers.accounting import Accounting, instrument_sdk_client
from sherd_agent.providers.base import ChatMessage, Completion, ProviderError

API_KEY_ENV = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_S = 120.0


def available(api_key_env: str = API_KEY_ENV) -> bool:
    return bool(os.environ.get(api_key_env))


class AnthropicProvider:
    """Messages API with structured output; `options` (effort, fallbacks) apply when given."""

    name = "anthropic"
    remote = True

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        max_tokens: int = 8192,
        options: Mapping[str, Any] | None = None,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.options = dict(options or {})
        self.accounting = Accounting()
        http_client = http_client or httpx2.Client(timeout=TIMEOUT_S)
        instrument_sdk_client(http_client, self.accounting)
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT_S, max_retries=2, http_client=http_client
        )

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        before = self.accounting.snapshot()
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns: list[BetaMessageParam] = [
            {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
        ]
        output_config: dict[str, Any] = {}
        if effort := self.options.get("effort"):
            output_config["effort"] = effort
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": dict(json_schema)}
        extra: dict[str, Any] = {}
        if fallbacks := self.options.get("fallbacks"):
            extra = {"fallbacks": fallbacks, "betas": [FALLBACK_BETA]}
        try:
            raw = self._client.beta.messages.with_raw_response.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system or anthropic.omit,
                messages=turns,
                output_config=output_config or anthropic.omit,  # type: ignore[arg-type]  # TypedDict built incrementally
                **extra,
            )
            message = raw.parse()
        except anthropic.AuthenticationError:
            raise ProviderError(f"anthropic rejected the API key in ${API_KEY_ENV}") from None
        except anthropic.NotFoundError:
            raise ProviderError(f"anthropic has no model {self.model!r}") from None
        except anthropic.RateLimitError:
            raise ProviderError("anthropic rate limit reached; try again later") from None
        except anthropic.APIStatusError as error:
            raise ProviderError(f"anthropic returned HTTP {error.status_code}") from None
        except anthropic.APIConnectionError:
            raise ProviderError("anthropic is not reachable") from None
        self.accounting.input_tokens += message.usage.input_tokens
        self.accounting.output_tokens += message.usage.output_tokens
        if message.stop_reason == "refusal":
            raise ProviderError("the model declined this request")
        if message.stop_reason == "max_tokens":
            raise ProviderError("the model ran out of output tokens")
        text = "".join(block.text for block in message.content if block.type == "text")
        return Completion(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            bytes_sent=self.accounting.bytes_sent - before.bytes_sent,
            bytes_received=self.accounting.bytes_received - before.bytes_received,
            requests=self.accounting.requests - before.requests,
        )
