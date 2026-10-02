"""Shared request dependencies: settings, the read-only store, UI copy and templates.

Page routes (WP-15) build on these; nothing here writes to the database.
"""

import os
import re
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sherd_core import Store

PACKAGE = files("sherd_web")
TEMPLATES_DIR = Path(str(PACKAGE.joinpath("templates")))
STATIC_DIR = Path(str(PACKAGE.joinpath("static")))

# One entry per v1 page (PLAN §9). WP-15 adds the routes; the shell only links to them.
# `table` names the fact table whose row count is shown next to the entry.
NAV: tuple[tuple[str, str, str, str | None], ...] = (
    ("/", "Home", "i-home", None),
    ("/messages", "Messages", "i-messages", "messages"),
    ("/music", "Music", "i-music", "media_plays"),
    ("/money", "Money", "i-money", "transactions"),
    ("/timeline", "Timeline", "i-timeline", None),
    ("/ask", "Ask", "i-ask", None),
    ("/wrapped", "Wrapped", "i-wrapped", None),
    ("/settings", "Settings", "i-settings", None),
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    demo: bool


def settings(request: Request) -> Settings:
    value: Settings = request.app.state.settings
    return value


def open_store(settings: Annotated[Settings, Depends(settings)]) -> Generator[Store | None]:
    """A read-only store for one request, or None when there is no database yet.

    Opened per request so the app never holds DuckDB's file lock between requests,
    which keeps `sherd dig` usable while the web UI is running.
    """
    if not settings.db_path.is_file():
        yield None
        return
    with Store.open(settings.db_path, read_only=True) as store:
        yield store


StoreDep = Annotated[Store | None, Depends(open_store)]
SettingsDep = Annotated[Settings, Depends(settings)]


def local_zone() -> str:
    """The system's IANA zone: `$TZ`, else the `/etc/localtime` link, else UTC."""
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if "zoneinfo/" in link:
        candidates.append(link.split("zoneinfo/", 1)[1])
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    return "UTC"


# ---- Copy -------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@cache
def copy() -> dict[str, Any]:
    data = yaml.safe_load(PACKAGE.joinpath("copy.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("copy.yaml must hold a mapping")
    return data


def t(key: str, **values: object) -> str:
    """The copy at dotted `key`, with `{placeholders}` filled from `values`.

    Unknown placeholders stay as they are, so a missing value shows up in review
    instead of raising mid-render.
    """
    node: Any = copy()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"copy.yaml has no {key!r}")
        node = node[part]
    if not isinstance(node, str):
        raise KeyError(f"copy.yaml {key!r} is not a string")
    return _PLACEHOLDER.sub(lambda m: str(values.get(m[1], m[0])), node)


# ---- Formatting -------------------------------------------------------------------------------


def number(value: object) -> str:
    """Thousands separators for counts; at most one decimal for measures."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() else f"{value:,.1f}"
    return str(value)


def compact(value: int) -> str:
    """48210 -> 48.2K, for the nav counts."""
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= size:
            text = f"{value / size:.1f}".removesuffix(".0")
            return f"{text}{suffix}"
    return str(value)


def cell(value: object, column: str = "") -> str:
    """A table cell; years and other `*year` columns keep their digits together."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if column.lower().endswith("year") and isinstance(value, int):
        return str(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return number(value)
    return "" if value is None else str(value)


def home_path(path: Path) -> str:
    """`path` with the home directory shown as `~`."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


# ---- Templates --------------------------------------------------------------------------------

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["t"] = t
templates.env.globals["NAV"] = NAV
templates.env.filters["number"] = number
templates.env.filters["compact"] = compact
templates.env.filters["cell"] = cell
templates.env.filters["home_path"] = home_path
templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False, "sort_keys": False}


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page inside the shell; the shell reads `settings`, `counts` and `path`."""
    state: Settings = request.app.state.settings
    ctx: dict[str, Any] = {"settings": state, "counts": {}, "path": request.url.path}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def is_current(path: str, href: str) -> bool:
    return path == href if href == "/" else path == href or path.startswith(href + "/")


templates.env.globals["is_current"] = is_current
