"""OpenAI chat models through the official SDK. Remote: needs consent and `OPENAI_API_KEY`."""

import os
from collections.abc import Mapping, Sequence
from typing import Any

import httpx2
import openai
from openai.types.chat import ChatCompletionMessageParam

from sherd_agent.providers.base import ChatMessage, Completion, ProviderError

API_KEY_ENV = "OPENAI_API_KEY"
TIMEOUT_S = 120.0


def available(api_key_env: str = API_KEY_ENV) -> bool:
    return bool(os.environ.get(api_key_env))


class OpenAIProvider:
    """Chat Completions in JSON mode; the caller validates the JSON against its own schema."""

    name = "openai"
    remote = True

    def __init__(
        self, model: str, *, api_key: str, http_client: httpx2.Client | None = None
    ) -> None:
        self.model = model
        self._client = openai.OpenAI(
            api_key=api_key, timeout=TIMEOUT_S, max_retries=2, http_client=http_client
        )

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        turns: list[ChatCompletionMessageParam] = [
            {"role": m["role"], "content": m["content"]}  # type: ignore[misc]  # role is a union of the SDK's per-role TypedDicts
            for m in messages
        ]
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=turns,
                response_format={"type": "json_object"} if json_schema is not None else openai.omit,
            )
            completion = raw.parse()
        except openai.AuthenticationError:
            raise ProviderError(f"openai rejected the API key in ${API_KEY_ENV}") from None
        except openai.NotFoundError:
            raise ProviderError(f"openai has no model {self.model!r}") from None
        except openai.RateLimitError:
            raise ProviderError("openai rate limit reached; try again later") from None
        except openai.APIStatusError as error:
            raise ProviderError(f"openai returned HTTP {error.status_code}") from None
        except openai.APIConnectionError:
            raise ProviderError("openai is not reachable") from None
        if not completion.choices:
            raise ProviderError("openai returned no choices")
        choice = completion.choices[0]
        if choice.message.refusal:
            raise ProviderError("the model declined this request")
        usage = completion.usage
        return Completion(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            bytes_sent=len(raw.http_request.content),
            bytes_received=len(raw.http_response.content),
        )
