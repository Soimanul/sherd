"""The ask pipeline (Contract D): question -> plan (dig, SQL or refusal) -> rows -> answer."""

import csv
import io
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from importlib.resources import files
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sherd_core import Catalog, Store, load_catalog
from sherd_insights import Dig, DigParams, charts, registry
from sherd_insights.charts import json_value, records

from sherd_agent import sql_guard
from sherd_agent.llm import LLM, Usage
from sherd_agent.providers import ChatMessage

logger = logging.getLogger("sherd.agent")

ANSWER_ROWS = 50
CATEGORICAL_CHART_MAX = 50


class AskError(Exception):
    """The question could not be answered; `sql` holds the last query tried, if any."""

    def __init__(self, message: str, *, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql


class PlanError(ValueError):
    """The model's reply is not a valid plan."""


# -- the plan ----------------------------------------------------------------------------------


class SqlPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1)


class DigPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dig: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class RefusePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refuse: str = Field(min_length=1)


Plan = SqlPlan | DigPlan | RefusePlan
_PLAN = TypeAdapter[Plan](Plan)
_DIG_PARAMS = TypeAdapter(DigParams)
DIG_PARAM_NAMES = tuple(f.name for f in fields(DigParams))


def _strip_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_schema(v) for k, v in node.items() if k not in ("title", "default")}
    if isinstance(node, list):
        return [_strip_schema(v) for v in node]
    return node


def plan_schema() -> dict[str, Any]:
    """JSON schema of the three plan shapes, self-contained (no $ref) for structured output."""
    params = _strip_schema(_DIG_PARAMS.json_schema())
    params.pop("required", None)
    params["additionalProperties"] = False

    def shape(name: str, value: dict[str, Any], extra: dict[str, Any] | None = None) -> Any:
        properties = {name: value, **(extra or {})}
        return {
            "type": "object",
            "properties": properties,
            "required": [name],
            "additionalProperties": False,
        }

    return {
        "anyOf": [
            shape("sql", {"type": "string"}),
            shape("dig", {"type": "string"}, {"params": params}),
            shape("refuse", {"type": "string"}),
        ]
    }


def parse_plan(text: str) -> Plan:
    """Parse and validate a model reply; tolerates a Markdown code fence around the JSON."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise PlanError(f"the reply is not JSON ({error.msg})") from None
    try:
        return _PLAN.validate_python(data)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()[:6]
        )
        raise PlanError(
            'the reply must be exactly one of {"sql": ...}, {"dig": ..., "params": {...}} or '
            f'{{"refuse": ...}} ({problems})'
        ) from None


def dig_params(raw: dict[str, Any], tz: str) -> DigParams:
    """Validate a plan's dig params into DigParams; `tz` fills in when the plan leaves it out."""
    unknown = sorted(set(raw) - set(DIG_PARAM_NAMES))
    if unknown:
        raise PlanError(f"unknown dig params {unknown}; allowed: {', '.join(DIG_PARAM_NAMES)}")
    try:
        params = _DIG_PARAMS.validate_python({"tz": tz, **raw})
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        raise PlanError(f"invalid dig params: {problems}") from None
    try:
        ZoneInfo(params.tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise PlanError(f"tz must be an IANA time zone, got {params.tz!r}") from None
    if params.top_n < 1:
        raise PlanError("top_n must be at least 1")
    if params.date_from and params.date_to and params.date_from > params.date_to:
        raise PlanError("date_from must be on or before date_to")
    return params


# -- prompt context ----------------------------------------------------------------------------


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def catalog_context(store: Store, catalog: Catalog) -> str:
    """Conventions, then each table with its row count, time range and column descriptions."""
    existing = {
        str(row["table_name"])
        for row in store.query(
            "SELECT table_name FROM duckdb_tables()"
            " WHERE database_name = current_database() AND schema_name = 'main'"
        ).to_pylist()
    }
    lines = [catalog.description, "", "Conventions:"]
    lines += [f"- {convention}" for convention in catalog.conventions]
    lines += ["", "Common provenance columns (present where a table says so):"]
    lines += [f"- {name}: {text}" for name, text in catalog.common_columns.items()]
    for table, doc in catalog.tables.items():
        if table not in existing:
            continue
        stats = f"SELECT count(*) AS n FROM main.{_quote(table)}"
        if "ts" in doc.columns:
            stats = f"SELECT count(*) AS n, min(ts) AS lo, max(ts) AS hi FROM main.{_quote(table)}"
        row = store.query(stats).to_pylist()[0]
        summary = f"{row['n']} rows"
        if row.get("lo") is not None:
            summary += f"; ts from {row['lo'].isoformat()} to {row['hi'].isoformat()}"
        common = all(doc.columns.get(k) == v for k, v in catalog.common_columns.items())
        lines += ["", f"### {table} ({summary})", doc.description, "Columns:"]
        if common:
            lines.append("- (the common provenance columns)")
        lines += [
            f"- {name}: {text}"
            for name, text in doc.columns.items()
            if not (common and name in catalog.common_columns)
        ]
    return "\n".join(lines)


def digs_context(digs: Sequence[Dig]) -> str:
    if not digs:
        return "None available for this database."
    return "\n".join(f"- {dig.id}: {dig.title} (uses {', '.join(dig.requires)})" for dig in digs)


def _prompt(name: str) -> str:
    return files("sherd_agent").joinpath(f"prompts/{name}.md").read_text(encoding="utf-8")


def plan_prompt(store: Store, digs: Sequence[Dig], *, tz: str, today: date) -> str:
    """The planning system prompt, exactly as sent to the provider."""
    params = ", ".join(DIG_PARAM_NAMES)
    replacements = {
        "{{dig_params}}": f"{params} (dates as YYYY-MM-DD; granularity day|week|month|year)",
        "{{today}}": today.isoformat(),
        "{{tz}}": tz,
        "{{catalog}}": catalog_context(store, load_catalog()),
        "{{digs}}": digs_context(digs),
    }
    text = _prompt("plan")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def rows_csv(table: pa.Table, limit: int = ANSWER_ROWS) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(table.column_names)
    for row in records(table.slice(0, limit)):
        writer.writerow(["" if row[n] is None else row[n] for n in table.column_names])
    return out.getvalue()


# -- charts ------------------------------------------------------------------------------------


def _numeric(kind: pa.DataType) -> bool:
    return bool(
        pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)
    )


def _temporal(kind: pa.DataType) -> bool:
    return bool(pa.types.is_timestamp(kind) or pa.types.is_date(kind))


def _categorical(kind: pa.DataType) -> bool:
    return bool(pa.types.is_string(kind) or pa.types.is_large_string(kind))


def chart_for(table: pa.Table) -> dict[str, Any] | None:
    """A line (temporal x) or bar (categorical x) chart for a two-column result, else None."""
    if table.num_columns != 2 or table.num_rows == 0:
        return None
    a, b = table.schema
    for x, y in ((a, b), (b, a)):
        if not _numeric(y.type):
            continue
        if _temporal(x.type):
            return charts.line(table, x.name, y.name)
        if _categorical(x.type) and table.num_rows <= CATEGORICAL_CHART_MAX:
            return charts.bar(table, x.name, y.name)
    return None


# -- the pipeline ------------------------------------------------------------------------------


@dataclass
class AskResult:
    question: str
    provider: str
    model: str
    remote: bool
    kind: Literal["sql", "dig", "refuse"]
    sql: str | None = None
    dig: str | None = None
    params: dict[str, Any] | None = None
    table: pa.Table | None = None
    truncated: bool = False
    answer: str = ""
    refusal: str | None = None
    chart: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def row_count(self) -> int:
        return self.table.num_rows if self.table is not None else 0

    @property
    def columns(self) -> list[str]:
        return self.table.column_names if self.table is not None else []

    def to_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "sql": self.sql,
            "dig": self.dig,
            "params": self.params,
            "columns": self.columns,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "rows": records(self.table) if self.table is not None else [],
            "answer": self.answer,
            "refusal": self.refusal,
            "chart": self.chart,
            "usage": asdict(self.usage) if self.remote else None,
        }


@dataclass(frozen=True)
class _Executed:
    table: pa.Table
    truncated: bool
    chart: dict[str, Any] | None


class Asker:
    def __init__(
        self,
        store: Store,
        llm: LLM,
        *,
        tz: str = "UTC",
        today: date | None = None,
        digs: Sequence[Dig] | None = None,
        row_budget: int = sql_guard.ROW_BUDGET,
        time_budget_s: float = sql_guard.TIME_BUDGET_S,
    ) -> None:
        self.store = store
        self.llm = llm
        self.tz = tz
        self.today = today or date.today()
        self.digs = {d.id: d for d in (registry.available(store) if digs is None else digs)}
        self.row_budget = row_budget
        self.time_budget_s = time_budget_s

    def system_prompt(self) -> str:
        return plan_prompt(self.store, list(self.digs.values()), tz=self.tz, today=self.today)

    def ask(self, question: str) -> AskResult:
        question = question.strip()
        if not question:
            raise AskError('ask a question, e.g. sherd ask "who did I message most in 2024?"')
        provider = self.llm.provider
        result = AskResult(question, provider.name, provider.model, provider.remote, "refuse")
        result.usage = self.llm.usage
        messages: list[ChatMessage] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": question},
        ]
        corrected = False
        while True:
            plan, reply = self._plan(messages)
            result.sql = result.dig = result.params = None
            try:
                if isinstance(plan, RefusePlan):
                    result.kind, result.refusal = "refuse", plan.refuse
                    logger.info("ask: refused")
                    return result
                if isinstance(plan, DigPlan):
                    result.kind, result.dig = "dig", plan.dig
                    executed = self._run_dig(plan, result)
                else:
                    result.kind, result.sql = "sql", plan.sql
                    executed = self._run_sql(plan.sql)
                break
            except (ValueError, sql_guard.QueryTimeoutError, duckdb.Error) as error:
                # ValueError covers PlanError, SqlRejectedError and dig parameter checks.
                if corrected:
                    raise AskError(f"the query failed: {error}", sql=result.sql) from None
                corrected = True
                logger.info(
                    "ask: execution failed (%s); asking for a correction", type(error).__name__
                )
                messages += [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": f"Running that plan failed: {error}\n"
                        "Reply with one corrected JSON object.",
                    },
                ]
        result.table, result.truncated, result.chart = (
            executed.table,
            executed.truncated,
            executed.chart,
        )
        result.answer = self._answer(result)
        logger.info("ask: %s plan, %d rows", result.kind, result.row_count)
        return result

    def _plan(self, messages: list[ChatMessage]) -> tuple[Plan, str]:
        schema = plan_schema()
        for attempt in range(2):
            reply = self.llm.complete(messages, schema).text
            try:
                return parse_plan(reply), reply
            except PlanError as error:
                if attempt:
                    raise AskError(
                        f"the model did not return a valid plan, even after a retry: {error}"
                    ) from None
                logger.info("ask: invalid plan; retrying once")
                messages += [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": f"Your reply was not a valid plan: {error}\n"
                        "Reply with exactly one JSON object and nothing else.",
                    },
                ]
        raise AssertionError("unreachable")

    def _run_dig(self, plan: DigPlan, result: AskResult) -> _Executed:
        dig = self.digs.get(plan.dig)
        if dig is None:
            raise PlanError(
                f"unknown dig {plan.dig!r}; available: {', '.join(self.digs) or 'none'}"
            )
        params = dig_params(plan.params, self.tz)
        result.params = json_value(asdict(params))
        computed = dig.compute(self.store, params)
        table = computed.data
        truncated = table.num_rows > self.row_budget
        return _Executed(table.slice(0, self.row_budget), truncated, computed.chart)

    def _run_sql(self, sql: str) -> _Executed:
        run = sql_guard.run(
            self.store, sql, row_budget=self.row_budget, time_budget_s=self.time_budget_s
        )
        return _Executed(run.table, run.truncated, chart_for(run.table))

    def _answer(self, result: AskResult) -> str:
        if result.kind == "dig":
            query = f"dig {result.dig} with params {json.dumps(result.params, sort_keys=True)}"
        else:
            query = result.sql or ""
        count = f"{result.row_count}" + (
            " (truncated at the row budget)" if result.truncated else ""
        )
        shown = min(result.row_count, ANSWER_ROWS)
        table = result.table if result.table is not None else pa.table({})
        content = (
            f"Question: {result.question}\n\nQuery: {query}\n\nRow count: {count}\n\n"
            f"First {shown} rows as CSV:\n{rows_csv(table)}"
        )
        messages: list[ChatMessage] = [
            {"role": "system", "content": _prompt("answer")},
            {"role": "user", "content": content},
        ]
        return self.llm.complete(messages).text.strip()


def ask(question: str, store: Store, llm: LLM, **options: Any) -> AskResult:
    """Answer `question` from `store` (open it read-only and sandboxed) with `llm`."""
    return Asker(store, llm, **options).ask(question)
