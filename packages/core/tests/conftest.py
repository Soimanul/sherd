from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sherd_core import Message, Store

BASE_TS = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)

MessageFactory = Callable[..., Message]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / "life.duckdb"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open(db_path) as opened:
        yield opened


@pytest.fixture
def make_message() -> MessageFactory:
    def make(i: int, **overrides: Any) -> Message:
        fields: dict[str, Any] = {
            "source_file": "chats/Chat with Ana.txt",
            "source_row_id": f"m{i:06d}",
            "chat_id": "chat-1",
            "chat_name": "Ana",
            "chat_kind": "direct",
            "sender_id": "whatsapp:+15550100",
            "sender_name": "Ana",
            "is_from_me": i % 2 == 0,
            "ts": BASE_TS + timedelta(seconds=i),
            "text": f"synthetic message {i}",
            "kind": "text",
        }
        fields.update(overrides)
        return Message(**fields)

    return make
