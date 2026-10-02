from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sherd_core import Message, Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "life.duckdb") as opened:
        yield opened


def message(
    key: str, ts: str, *, me: bool = True, contact: str = "Contact A", **extra: Any
) -> Message:
    fields: dict[str, Any] = {
        "source_file": "synthetic.txt",
        "source_row_id": key,
        "chat_id": contact,
        "chat_name": contact,
        "chat_kind": "direct",
        "is_from_me": me,
        "ts": datetime.fromisoformat(ts),
        "text": "hello 😀",
        "kind": "text",
    }
    fields.update(extra)
    return Message(**fields)


def insert(store: Store, rows: list[Message], *, source: str = "synthetic") -> None:
    key = store.begin_import(source, "1", "synthetic", "UTC")
    store.upsert(key, source, rows)
    store.finish_import(key, "succeeded")
