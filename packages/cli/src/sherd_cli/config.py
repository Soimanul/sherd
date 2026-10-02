"""User settings in `$SHERD_HOME/config.json`."""

import json
import os
from pathlib import Path
from typing import Any

from sherd_core.paths import sherd_home


def config_path() -> Path:
    return sherd_home() / "config.json"


def load() -> dict[str, Any]:
    """The settings, or `{}` when there is no config file yet."""
    path = config_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    connectors = data.get("connectors", {})
    if not isinstance(connectors, dict):
        raise ValueError("config connectors must be an object")
    for connector_id, settings in connectors.items():
        if not isinstance(settings, dict):
            raise ValueError(f"config connector {connector_id!r} must be an object")
        identities = settings.get("me", [])
        if not isinstance(identities, list) or any(
            not isinstance(value, str) for value in identities
        ):
            raise ValueError(f"config connector {connector_id!r} me must be a list of strings")
    return data


def save(data: dict[str, Any]) -> None:
    """Write the settings atomically."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.tmp")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(partial, path)


def self_identities(connector_id: str) -> list[str]:
    """The `--me` values last given for `connector_id`."""
    connectors = load().get("connectors", {})
    values = connectors.get(connector_id, {}).get("me", [])
    return [str(value) for value in values]


def set_self_identities(connector_id: str, values: list[str]) -> None:
    data = load()
    data.setdefault("connectors", {}).setdefault(connector_id, {})["me"] = list(values)
    save(data)
