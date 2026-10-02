"""A canned-answer provider for tests and demos; it never touches the network."""

import json
import os
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from sherd_agent.providers.base import ChatMessage, Completion, body_size

SCRIPT_ENV = "SHERD_AGENT_STUB"
NO_PLAN = {"refuse": "the stub provider has no canned plan for this question"}


class StubProvider:
    """Answers planning calls from `plans` (question -> plan) and answer calls with `answer`.

    `replies` are returned first, in order, whatever the call; tests use them to script invalid
    output. A planning call is one that passes a JSON schema.
    """

    name = "stub"
    remote = False

    def __init__(
        self,
        plans: Mapping[str, Mapping[str, Any] | str] | None = None,
        *,
        replies: Iterable[str] = (),
        answer: str = "Stub answer: see the rows below.",
        model: str = "canned",
    ) -> None:
        self.model = model
        self.plans = dict(plans or {})
        self.replies = deque(replies)
        self.answer = answer
        self.calls: list[list[ChatMessage]] = []

    @classmethod
    def from_env(cls, model: str = "canned") -> "StubProvider":
        """Load plans from the YAML/JSON file named by `$SHERD_AGENT_STUB`, if set."""
        path = os.environ.get(SCRIPT_ENV)
        if not path:
            return cls(model=model)
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{SCRIPT_ENV} must name a mapping of question to plan")
        return cls({str(k): v for k, v in data.items()}, model=model)

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        self.calls.append(list(messages))
        if self.replies:
            text = self.replies.popleft()
        elif json_schema is None:
            text = self.answer
        else:
            question = next(m["content"] for m in messages if m["role"] == "user")
            plan = self.plans.get(question.strip(), NO_PLAN)
            text = plan if isinstance(plan, str) else json.dumps(plan)
        return Completion(
            text=text,
            input_tokens=0,
            output_tokens=0,
            bytes_sent=body_size(messages),
            bytes_received=len(text.encode()),
        )
