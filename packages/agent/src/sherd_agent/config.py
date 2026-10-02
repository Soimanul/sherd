"""The agent's settings: the "ask" object in `$SHERD_HOME/config.json`.

Other keys in the file belong to other parts of sherd and are written back untouched.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sherd_core.paths import sherd_home


def config_path() -> Path:
    return sherd_home() / "config.json"


def _load_all() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return data


def load() -> dict[str, Any]:
    """The "ask" object: optional `provider`, `model` and `consent` (provider -> ISO time)."""
    ask = _load_all().get("ask", {})
    if not isinstance(ask, dict):
        raise ValueError("config ask must be an object")
    for key in ("provider", "model"):
        if key in ask and not isinstance(ask[key], str):
            raise ValueError(f"config ask.{key} must be a string")
    if not isinstance(ask.get("consent", {}), dict):
        raise ValueError("config ask.consent must be an object")
    return ask


def save(ask: dict[str, Any]) -> None:
    """Replace the "ask" object atomically, keeping every other key."""
    data = _load_all()
    data["ask"] = ask
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.tmp")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(partial, path)


def has_consent(provider: str) -> bool:
    return provider in load().get("consent", {})


def record_consent(provider: str) -> None:
    ask = load()
    ask.setdefault("consent", {})[provider] = datetime.now(UTC).isoformat(timespec="seconds")
    save(ask)
