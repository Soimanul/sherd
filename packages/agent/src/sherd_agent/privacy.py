"""The privacy counter in `$SHERD_HOME/privacy.json`: what each provider was sent and returned.

Layout (read by the Settings page):
    {"version": 1,
     "remote": {"anthropic": {"requests": 3, "bytes_sent": 41250, "bytes_received": 2210,
                              "input_tokens": 9800, "output_tokens": 410}},
     "local": {"ollama": {...}}}
Remote and local providers are counted in separate sections, so "0 bytes sent" means nothing
left the machine.
"""

import json
import os
from pathlib import Path
from typing import Any

from sherd_core.paths import sherd_home

from sherd_agent.providers.base import Completion

COUNTERS = ("requests", "bytes_sent", "bytes_received", "input_tokens", "output_tokens")


def privacy_path() -> Path:
    return sherd_home() / "privacy.json"


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or privacy_path()
    if not path.is_file():
        return {"version": 1, "remote": {}, "local": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    data.setdefault("version", 1)
    data.setdefault("remote", {})
    data.setdefault("local", {})
    return data


def record(provider: str, remote: bool, completion: Completion, path: Path | None = None) -> None:
    """Add one request and its byte and token counts to `provider`'s totals."""
    path = path or privacy_path()
    data = load(path)
    totals = data["remote" if remote else "local"].setdefault(provider, {})
    increments = {
        "requests": 1,
        "bytes_sent": completion.bytes_sent,
        "bytes_received": completion.bytes_received,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }
    for key in COUNTERS:
        totals[key] = int(totals.get(key, 0)) + increments[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.tmp")
    partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def remote_bytes_sent(path: Path | None = None) -> int:
    """Bytes sent to all remote providers so far."""
    return sum(int(v.get("bytes_sent", 0)) for v in load(path)["remote"].values())
