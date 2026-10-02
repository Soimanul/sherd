"""Where sherd keeps its data."""

import os
from pathlib import Path


def sherd_home() -> Path:
    """`$SHERD_HOME`, or `~/.sherd` when unset or empty."""
    value = os.environ.get("SHERD_HOME")
    return Path(value).expanduser() if value else Path.home() / ".sherd"


def default_db_path() -> Path:
    return sherd_home() / "life.duckdb"
