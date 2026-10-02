"""Ask: a question box over the same pipeline as `sherd ask` (PLAN §7), on a sandboxed store.

POST only, with the per-process CSRF token and a same-origin check (`_pages.posted_form`).
A remote provider gets nothing until the user allows it on the consent card, which is
recorded in config.json exactly as the CLI records it.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sherd_agent import config, providers
from sherd_agent.ask import ANSWER_CELL_CHARS, ANSWER_CSV_BYTES, ANSWER_ROWS, Asker, AskError
from sherd_agent.llm import LLM, Choice, NoProviderError, OfflineRefusedError, resolve
from sherd_agent.providers import Provider, ProviderError
from sherd_agent.providers.netguard import OfflineError, offline_guard
from sherd_agent.sql_guard import ROW_BUDGET
from sherd_core import Store
from sherd_core.store import StoreError
from sherd_insights import DigResult
from sherd_insights.charts import json_value, records

from sherd_web.app import Card
from sherd_web.deps import SettingsDep, StoreDep, copy, local_zone, render, t
from sherd_web.routes._pages import CSRF_TOKEN, Form, numeric, partial, shell

router = APIRouter()
logger = logging.getLogger("sherd.web")

RESULT_ID = "ask-result"
PREVIEW_ROWS = 20
QUESTION_LIMIT = 2000
# One question at a time: the offline guard patches the process's sockets while it runs.
_ASK_LOCK = threading.Lock()

# Seams the tests replace: how providers are found and built (the CLI uses the same ones).
PROBE: Callable[[str, providers.Settings], bool] = providers.available
CREATE: Callable[[str, str, providers.Settings], Provider] = providers.create


# ---- Provider status --------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    """Which provider a question would go to, or why none would."""

    choice: Choice | None = None
    consented: bool = False
    no_provider: str | None = None
    refused: str | None = None
    broken: str | None = None  # config.json could not be read


def provider_status(*, offline: bool = False) -> Status:
    settings = providers.load_settings()
    try:
        choice = resolve(settings, config=config.load(), offline=offline, probe=PROBE)
        return Status(choice, consented=not choice.remote or config.has_consent(choice.provider))
    except NoProviderError as error:
        return Status(no_provider=str(error))
    except OfflineRefusedError as error:
        return Status(refused=str(error))
    except ValueError as error:
        return Status(broken=f"{config.config_path()}: {error}")


def ollama_model() -> str:
    return providers.load_settings().default_model("ollama") or "qwen2.5-coder:7b"


# ---- SQL display ------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"(?P<c>--[^\n]*|/\*.*?\*/)|(?P<s>'(?:[^']|'')*'?)|(?P<q>\"(?:[^\"]|\"\")*\"?)"
    r"|(?P<n>\b\d+(?:\.\d+)?\b)|(?P<w>[A-Za-z_][A-Za-z0-9_]*)|(?P<o>[^A-Za-z0-9_'\"\-/]+|.)",
    re.DOTALL,
)
_KEYWORDS = frozenset(
    [
        "select",
        "from",
        "where",
        "group",
        "by",
        "order",
        "having",
        "limit",
        "offset",
        "with",
        "as",
        "join",
        "left",
        "right",
        "inner",
        "outer",
        "full",
        "cross",
        "on",
        "and",
        "or",
        "not",
        "in",
        "is",
        "null",
        "like",
        "ilike",
        "between",
        "case",
        "when",
        "then",
        "else",
        "end",
        "distinct",
        "union",
        "all",
        "except",
        "intersect",
        "asc",
        "desc",
        "nulls",
        "first",
        "last",
        "over",
        "partition",
        "window",
        "filter",
        "qualify",
        "using",
        "exists",
        "any",
        "true",
        "false",
        "interval",
        "cast",
        "try_cast",
    ]
)


def sql_tokens(sql: str) -> list[tuple[str, str]]:
    """(css class, text) pieces for the SQL block: k keyword, f function, s string, n number,
    c comment, "" anything else. The template escapes every piece."""
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN.finditer(sql):
        kind = match.lastgroup or "o"
        text = match.group()
        if kind == "w":
            if text.lower() in _KEYWORDS:
                kind = "k"
            elif sql[match.end() : match.end() + 1] == "(":
                kind = "f"
            else:
                kind = ""
        elif kind in ("o", "q"):
            kind = ""
        if tokens and tokens[-1][0] == kind == "":
            tokens[-1] = ("", tokens[-1][1] + text)
        else:
            tokens.append((kind, text))
    return tokens


# ---- Result view ------------------------------------------------------------------------------


@dataclass
class View:
    """What the result region shows; every field is optional."""

    question: str = ""
    offline: bool = False
    status: Status = field(default_factory=Status)
    consent: dict[str, Any] | None = None
    cancelled: bool = False
    error: str | None = None
    error_kind: str = "danger"
    refusal: str | None = None
    sql: list[tuple[str, str]] | None = None
    sql_text: str | None = None
    dig: str | None = None
    params: str | None = None
    row_count: int = 0
    truncated: bool = False
    answer: str = ""
    chart: Card | None = None
    columns: list[tuple[str, bool]] = field(default_factory=list)
    rows: list[list[object]] = field(default_factory=list)
    usage: dict[str, int] | None = None
    provider: str = ""
    model: str = ""

    @property
    def has_result(self) -> bool:
        return self.sql is not None or self.dig is not None


def _chart_card(table: pa.Table, chart: dict[str, Any]) -> Card:
    spec = {k: v for k, v in chart.items() if k != "$schema"}
    names = ", ".join(table.column_names)
    summary = t("pages.ask.chart_summary", columns=names, count=f"{table.num_rows:,}")
    result = DigResult(table, chart, t("pages.ask.chart_narrative"), None, summary)
    return Card(
        "ask-chart", "ask", t("pages.ask.chart_title"), result, spec, [], [], table.num_rows
    )


def _preview(view: View, table: pa.Table) -> None:
    view.columns = [(f.name, numeric(f.type)) for f in table.schema]
    names = table.column_names
    view.rows = [[row[n] for n in names] for row in records(table.slice(0, PREVIEW_ROWS))]


def _consent(asker: Asker, choice: Choice, question: str) -> dict[str, Any]:
    return {
        "provider": choice.provider,
        "model": choice.model,
        "prompt": asker.system_prompt(),
        "question": question,
        "rows": ANSWER_ROWS,
        "kilobytes": ANSWER_CSV_BYTES // 1024,
        "cell_chars": ANSWER_CELL_CHARS,
        "digs": len(asker.digs),
    }


def _run(db_path: Path, question: str, *, offline: bool, consent: str, consent_for: str) -> View:
    """Resolve the provider, ask for consent if needed, then answer `question`."""
    view = View(question=question, offline=offline)
    guard: AbstractContextManager[None] = offline_guard() if offline else nullcontext()
    with _ASK_LOCK, guard:
        settings = providers.load_settings()
        try:
            choice = resolve(settings, config=config.load(), offline=offline, probe=PROBE)
        except OfflineRefusedError as error:
            view.status = Status(refused=str(error))
            return view
        except NoProviderError as error:
            view.status = Status(no_provider=str(error))
            return view
        view.status = Status(choice, consented=not choice.remote)
        view.provider, view.model = choice.provider, choice.model
        llm = LLM(CREATE(choice.provider, choice.model, settings))
        with Store.open(db_path, read_only=True, sandboxed=True) as store:
            asker = Asker(store, llm, tz=local_zone())
            if choice.remote and not config.has_consent(choice.provider):
                if consent == "cancel":
                    view.cancelled = True
                    return view
                if consent != "allow" or consent_for != choice.provider:
                    view.consent = _consent(asker, choice, question)
                    return view
                config.record_consent(choice.provider)
                logger.info("ask: consent recorded for provider=%s", choice.provider)
            view.status = Status(choice, consented=True)
            try:
                result = asker.ask(question)
            except AskError as error:
                view.error = t("pages.ask.failed", reason=str(error))
                if error.sql:
                    view.sql, view.sql_text = sql_tokens(error.sql), error.sql
                return view
    if result.remote:
        view.usage = {
            "requests": result.usage.requests,
            "bytes_sent": result.usage.bytes_sent,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
    if result.kind == "refuse":
        view.refusal = result.refusal or ""
        return view
    if result.kind == "dig":
        view.dig = result.dig
        view.params = json.dumps(json_value(result.params), ensure_ascii=False, sort_keys=True)
    else:
        view.sql, view.sql_text = sql_tokens(result.sql or ""), result.sql
    view.row_count, view.truncated, view.answer = result.row_count, result.truncated, result.answer
    if result.table is not None:
        _preview(view, result.table)
        if result.chart is not None:
            view.chart = _chart_card(result.table, result.chart)
    return view


# ---- Routes -----------------------------------------------------------------------------------


def _context(store: Store | None, view: View) -> dict[str, Any]:
    return shell(
        store,
        view=view,
        result_id=RESULT_ID,
        question_limit=QUESTION_LIMIT,
        preview_rows=PREVIEW_ROWS,
        row_budget=ROW_BUDGET,
        examples=copy()["empty"]["ask_no_history"]["examples"],
        ollama_model=ollama_model(),
    )


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, store: StoreDep, settings: SettingsDep) -> Response:
    context = _context(store, View(status=provider_status()))
    context["empty"] = store is None or not any(context["counts"].values())
    return render(request, "pages/ask.html", context)


@router.post("/ask", response_class=HTMLResponse)
def ask_post(request: Request, form: Form, store: StoreDep, settings: SettingsDep) -> Response:
    question = (form.get("example") or form.get("question", "")).strip()
    offline = form.get("offline") == "on"
    counts = store.table_counts() if store is not None else {}
    if store is None or not any(counts.values()):
        context = _context(store, View(question=question, offline=offline))
        return render(request, "pages/ask.html", {**context, "empty": True})
    if not question or len(question) > QUESTION_LIMIT:
        key = "pages.ask.too_long" if question else "pages.ask.no_question"
        view = View(question=question[:QUESTION_LIMIT], offline=offline, error_kind="warning")
        view.error = t(key, limit=f"{QUESTION_LIMIT:,}")
        view.status = provider_status(offline=offline)
    else:
        try:
            view = _run(
                settings.db_path,
                question,
                offline=offline,
                consent=form.get("consent", ""),
                consent_for=form.get("provider", ""),
            )
        except (OfflineError, ProviderError) as error:
            view = View(question=question, offline=offline, status=provider_status())
            view.error = t("pages.ask.provider_failed", reason=str(error))
        except (ValueError, OSError, StoreError, duckdb.Error) as error:
            logger.warning("ask failed: error=%s", type(error).__name__)
            view = View(question=question, offline=offline, status=provider_status())
            view.error = t("pages.ask.failed", reason=str(error))
    context = {**_context(store, view), "empty": False, "csrf": CSRF_TOKEN}
    if partial(request) == RESULT_ID:
        return render(request, "pages/_ask_result.html", context)
    return render(request, "pages/ask.html", context)
