"""The local web app: shell, Home, error pages and security headers (PLAN §9, WP-14b).

Page routes live in `sherd_web.routes` (WP-15): every module there that exposes
`router: fastapi.APIRouter` is included, so no WP edits a central list.
"""

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.types as pat
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sherd_core import Store
from sherd_core.store import StoreError
from sherd_insights import Dig, DigParams, DigResult, registry
from sherd_insights.charts import records
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sherd_web.deps import (
    STATIC_DIR,
    Settings,
    SettingsDep,
    StoreDep,
    local_zone,
    render,
)

logger = logging.getLogger("sherd.web")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# The server binds to loopback; checking Host as well stops DNS-rebinding pages
# from reading the database through the user's browser.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
TABLE_ROWS_SHOWN = 200
START_HERE = 3
# Home's preferred "start here" charts, by dig id; the rest are picked by source.
PREFERRED = ("messages.top_contacts_by_year", "messages.activity_heatmap")
TABLE_WORDS = {
    "messages": ("message", "messages"),
    "media_plays": ("play", "plays"),
    "transactions": ("transaction", "transactions"),
    "events": ("event", "events"),
    "locations": ("place", "places"),
}
DIG_ERRORS = (StoreError, duckdb.Error, ValueError, ArithmeticError, KeyError)


class SecurityHeaders:
    """Adds the CSP and friends to every response that passes through the app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                # Static files revalidate (cheap on loopback, never stale after an
                # upgrade); pages hold personal data, so they are never cached.
                static = str(scope["path"]).startswith("/static/")
                headers["Cache-Control"] = "no-cache" if static else "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ShellStatic(StaticFiles):
    """Static files minus the design-system preview, which has its own, looser CSP."""

    def get_path(self, scope: Scope) -> str:
        path = super().get_path(scope)
        # Compared case-insensitively: macOS file systems would serve "Design/" too.
        if [part.lower() for part in Path(path).parts[:1]] == ["design"]:
            raise StarletteHTTPException(status_code=404)
        return path


# ---- View models ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """What the chart-card and stat-tile macros render for one dig."""

    id: str
    dig_id: str
    title: str
    result: DigResult
    spec: dict[str, Any] | None
    columns: list[tuple[str, bool]]
    rows: list[list[object]]
    total_rows: int


def card(dig: Dig, result: DigResult) -> Card:
    """A dig result ready for `macros/components.html`.

    The spec keeps its inline data and theme; `$schema` is dropped because the
    page names its mode itself and must not reference remote URLs.
    """
    spec = None
    if result.chart is not None:
        spec = {k: v for k, v in result.chart.items() if k != "$schema"}
    data = result.data
    columns = [
        (field.name, _is_numeric(field.type))
        for field in data.schema
        if not field.name.startswith("__")
    ]
    names = [name for name, _ in columns]
    rows = [[row[name] for name in names] for row in records(data.slice(0, TABLE_ROWS_SHOWN))]
    return Card(
        id="dig-" + dig.id.replace(".", "-").replace("_", "-"),
        dig_id=dig.id,
        title=dig.title,
        result=result,
        spec=spec,
        columns=columns,
        rows=rows,
        total_rows=data.num_rows,
    )


def _is_numeric(arrow_type: pa.DataType) -> bool:
    return bool(
        pat.is_integer(arrow_type) or pat.is_floating(arrow_type) or pat.is_decimal(arrow_type)
    )


def compute(store: Store, digs: list[Dig], params: DigParams) -> tuple[list[Card], list[Dig]]:
    """Compute `digs`; a dig that fails is logged by id and returned separately."""
    cards: list[Card] = []
    failed: list[Dig] = []
    for dig in digs:
        try:
            cards.append(card(dig, dig.compute(store, params)))
        except DIG_ERRORS as error:
            logger.warning("dig failed: id=%s error=%s", dig.id, type(error).__name__)
            failed.append(dig)
    return cards, failed


def start_here(cards: list[Card], digs: dict[str, Dig], count: int = START_HERE) -> list[Card]:
    """Up to `count` charts: preferred ids first, then one per source table, then the rest."""
    charted = [c for c in cards if c.spec is not None]
    chosen = [c for dig_id in PREFERRED for c in charted if c.dig_id == dig_id]
    seen_tables = {digs[c.dig_id].requires[0] for c in chosen if digs[c.dig_id].requires}
    for c in charted:
        table = digs[c.dig_id].requires[0] if digs[c.dig_id].requires else ""
        if c not in chosen and table not in seen_tables:
            chosen.append(c)
            seen_tables.add(table)
    chosen += [c for c in charted if c not in chosen]
    return chosen[:count]


def summary(counts: dict[str, int]) -> str:
    """'22,348 messages, 14,665 plays and 2,886 transactions'."""
    parts = [
        f"{n:,} {TABLE_WORDS.get(table, (table, table))[n != 1]}"
        for table, n in counts.items()
        if n
    ]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ---- App --------------------------------------------------------------------------------------


def create_app(db_path: Path, *, demo: bool) -> FastAPI:
    """The web UI over the database at `db_path`, which is only ever opened read-only."""
    app = FastAPI(title="sherd", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = Settings(db_path=Path(db_path), demo=demo)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(SecurityHeaders)
    app.mount("/static", ShellStatic(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, store: StoreDep, settings: SettingsDep) -> Response:
        if store is None:
            return render(request, "shell/home.html", {"onboarding": True})
        counts = store.table_counts()
        available = registry.available(store)
        if not available:
            return render(request, "shell/home.html", {"onboarding": True, "counts": counts})
        cards, failed = compute(store, available, DigParams(tz=local_zone()))
        digs = {dig.id: dig for dig in available}
        return render(
            request,
            "shell/home.html",
            {
                "onboarding": False,
                "counts": counts,
                "summary": summary(counts),
                "tiles": [c for c in cards if c.result.headline is not None],
                "charts": start_here(cards, digs),
                "failed": failed,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        return render(
            request,
            "shell/error.html",
            {"status": error.status_code, "title": _error_title(error.status_code)},
            status_code=error.status_code,
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, error: Exception) -> Response:
        # Starlette sends this response outside the middleware stack, so the
        # headers are added here as well.
        logger.error("request failed: path=%s error=%s", request.url.path, type(error).__name__)
        detail = _error_detail(error)
        response = render(
            request,
            "shell/error.html",
            {"status": 500, "title": "Something went wrong", "detail": detail},
            status_code=500,
        )
        response.headers.update(SECURITY_HEADERS)
        response.headers["Cache-Control"] = "no-store"
        return response

    _include_routes(app)
    return app


def _error_title(status: int) -> str:
    return "Nothing dug up here" if status == 404 else "That request didn't work"


def _error_detail(error: Exception) -> str:
    if isinstance(error, duckdb.IOException):
        return "The database is locked or unreadable. Another sherd command may be using it."
    if isinstance(error, StoreError):
        return str(error)
    return "Details are in the terminal that runs sherd web."


def _include_routes(app: FastAPI) -> None:
    try:
        routes = importlib.import_module("sherd_web.routes")
    except ModuleNotFoundError as error:
        if error.name != "sherd_web.routes":
            raise
        return
    for info in pkgutil.iter_modules(routes.__path__, routes.__name__ + "."):
        router = getattr(importlib.import_module(info.name), "router", None)
        if router is not None:
            app.include_router(router)
