import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest
from sherd_agent.ask import (
    ANSWER_ROWS,
    Asker,
    AskError,
    PlanError,
    chart_for,
    dig_params,
    parse_plan,
    plan_schema,
    rows_csv,
)
from sherd_agent.llm import LLM
from sherd_agent.providers.stub import StubProvider
from sherd_core import Store

QUESTION = "How many messages are there?"
COUNT = json.dumps({"sql": "SELECT count(*) AS n FROM messages"})


def asker(store: Store, stub: StubProvider, **options: object) -> Asker:
    return Asker(store, LLM(stub), tz="Europe/Bucharest", today=date(2026, 10, 2), **options)  # type: ignore[arg-type]  # options are Asker keyword arguments


def test_invalid_json_is_retried_once_with_the_error(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=["Sure! Here is the SQL: SELECT 1", COUNT])
    result = asker(demo_store, stub).ask(QUESTION)
    assert result.kind == "sql"
    assert result.row_count == 1
    retry = stub.calls[1]
    assert retry[-2] == {"role": "assistant", "content": "Sure! Here is the SQL: SELECT 1"}
    assert "not JSON" in retry[-1]["content"]
    assert len(stub.calls) == 3  # plan, corrected plan, answer


def test_invalid_twice_is_a_clear_failure(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=["nope", '{"sql": "SELECT 1", "extra": true}'])
    with pytest.raises(AskError, match="did not return a valid plan, even after a retry"):
        asker(demo_store, stub).ask(QUESTION)
    assert len(stub.calls) == 2


def test_failing_sql_goes_back_to_the_model_once(demo_store: Store, home: Path) -> None:
    bad = json.dumps({"sql": "SELECT nope FROM messages"})
    stub = StubProvider(replies=[bad, COUNT])
    result = asker(demo_store, stub).ask(QUESTION)
    assert result.sql == "SELECT count(*) AS n FROM messages"
    assert "nope" in stub.calls[1][-1]["content"]
    assert "Running that plan failed" in stub.calls[1][-1]["content"]


def test_failing_twice_is_reported_with_the_sql(demo_store: Store, home: Path) -> None:
    bad = json.dumps({"sql": "SELECT nope FROM messages"})
    write = json.dumps({"sql": "DELETE FROM messages"})
    stub = StubProvider(replies=[bad, write])
    with pytest.raises(AskError, match="only SELECT/WITH reads") as caught:
        asker(demo_store, stub).ask(QUESTION)
    assert caught.value.sql == "DELETE FROM messages"


def test_timeouts_are_returned_to_the_model(demo_store: Store, home: Path) -> None:
    slow = json.dumps({"sql": "SELECT count(*) FROM range(100000000) a, range(100000000) b"})
    stub = StubProvider(replies=[slow, COUNT])
    result = asker(demo_store, stub, time_budget_s=0.3).ask(QUESTION)
    assert result.row_count == 1
    assert "longer than 0.3 s" in stub.calls[1][-1]["content"]


def test_truncation_is_reported(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=[json.dumps({"sql": "SELECT * FROM range(30) r(i)"})])
    result = asker(demo_store, stub, row_budget=10).ask(QUESTION)
    assert (result.row_count, result.truncated) == (10, True)
    answer_prompt = stub.calls[-1][-1]["content"]
    assert "Row count: 10 (truncated at the row budget)" in answer_prompt


def test_answer_prompt_sends_at_most_50_rows(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=[json.dumps({"sql": "SELECT * FROM range(80) r(i)"})])
    result = asker(demo_store, stub).ask(QUESTION)
    assert result.row_count == 80
    system, user = stub.calls[-1]
    assert system["content"].startswith("<!-- prompt: answer, version 1 -->")
    csv_part = user["content"].split("as CSV:\n", 1)[1]
    assert csv_part.splitlines() == ["i", *[str(i) for i in range(ANSWER_ROWS)]]
    assert f"Question: {QUESTION}" in user["content"]
    assert "Row count: 80" in user["content"]


def test_dig_plans_run_the_dig_with_validated_params(demo_store: Store, home: Path) -> None:
    plan = {"dig": "messages.volume_by_contact", "params": {"top_n": 2, "granularity": "year"}}
    stub = StubProvider(replies=[json.dumps(plan)])
    result = asker(demo_store, stub).ask(QUESTION)
    assert result.kind == "dig"
    assert result.params is not None
    assert result.params["top_n"] == 2
    assert result.params["tz"] == "Europe/Bucharest"  # the user's zone when the plan omits it
    assert result.chart is not None
    assert "Query: dig messages.volume_by_contact with params" in stub.calls[-1][-1]["content"]


def test_unknown_dig_or_bad_params_get_one_correction(demo_store: Store, home: Path) -> None:
    unknown = json.dumps({"dig": "messages.nothing"})
    stub = StubProvider(replies=[unknown, COUNT])
    assert asker(demo_store, stub).ask(QUESTION).kind == "sql"
    assert "unknown dig 'messages.nothing'" in stub.calls[1][-1]["content"]

    bad = json.dumps({"dig": "messages.volume_by_contact", "params": {"colour": "red"}})
    stub = StubProvider(replies=[bad, bad])
    with pytest.raises(AskError, match="unknown dig params"):
        asker(demo_store, stub).ask(QUESTION)


def test_refusals_skip_the_answer_call(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=[json.dumps({"refuse": "not in your data"})])
    result = asker(demo_store, stub).ask("What will the weather be?")
    assert (result.kind, result.refusal, result.table) == ("refuse", "not in your data", None)
    assert len(stub.calls) == 1


def test_empty_question(demo_store: Store, home: Path) -> None:
    with pytest.raises(AskError, match="ask a question"):
        asker(demo_store, StubProvider()).ask("   ")


def test_plan_prompt_carries_catalog_counts_ranges_and_digs(demo_store: Store) -> None:
    prompt = asker(demo_store, StubProvider()).system_prompt()
    counts = demo_store.table_counts()
    assert prompt.startswith("<!-- prompt: plan, version 1 -->")
    assert f"### messages ({counts['messages']} rows; ts from 2023-" in prompt
    assert "### contacts (0 rows)" in prompt
    assert "Money is DECIMAL(18,2)" in prompt
    assert "- messages.volume_by_contact: Message volume by contact" in prompt
    assert "Today's date is 2026-10-02; the person's time zone is Europe/Bucharest." in prompt
    assert "{{" not in prompt


def test_parse_plan_shapes() -> None:
    assert parse_plan('{"sql": "SELECT 1"}').sql == "SELECT 1"  # type: ignore[union-attr]  # SqlPlan
    assert parse_plan('```json\n{"refuse": "no"}\n```').refuse == "no"  # type: ignore[union-attr]  # RefusePlan
    assert parse_plan('{"dig": "x"}').params == {}  # type: ignore[union-attr]  # DigPlan
    for bad in ("[]", '{"sql": ""}', '{"sql": "a", "dig": "b"}', '{"answer": 1}', "SELECT 1"):
        with pytest.raises(PlanError):
            parse_plan(bad)


def test_dig_params_validation() -> None:
    params = dig_params({"date_from": "2025-01-01", "top_n": 3}, "Europe/Bucharest")
    assert (params.date_from, params.top_n, params.tz) == (date(2025, 1, 1), 3, "Europe/Bucharest")
    assert dig_params({"tz": "UTC"}, "Europe/Bucharest").tz == "UTC"
    for bad in (
        {"granularity": "hour"},
        {"tz": "Mars/Olympus"},
        {"top_n": 0},
        {"date_from": "2025-02-01", "date_to": "2025-01-01"},
        {"date_from": "yesterday"},
    ):
        with pytest.raises(PlanError):
            dig_params(bad, "UTC")


def test_plan_schema_is_self_contained() -> None:
    schema = plan_schema()
    text = json.dumps(schema)
    assert "$ref" not in text
    assert [option["required"] for option in schema["anyOf"]] == [["sql"], ["dig"], ["refuse"]]
    params = schema["anyOf"][1]["properties"]["params"]
    assert params["additionalProperties"] is False
    assert {"date_from", "date_to", "top_n", "granularity", "tz"} <= set(params["properties"])


def test_chart_rules() -> None:
    temporal = pa.table({"month": [date(2025, 1, 1), date(2025, 2, 1)], "n": [3, 4]})
    categorical = pa.table({"artist": ["A", "B"], "minutes": [1.5, 2.0]})
    chart = chart_for(temporal)
    assert chart is not None
    assert chart["mark"] == "line"
    assert chart["encoding"]["x"]["field"] == "month"
    bar = chart_for(categorical)
    assert bar is not None
    assert bar["mark"] == "bar"
    assert chart_for(pa.table({"n": [1, 2], "artist": ["A", "B"]})) is not None
    assert chart_for(pa.table({"year": [2024, 2025], "n": [1, 2]})) is None  # two numbers
    assert chart_for(pa.table({"a": ["x"], "b": ["y"]})) is None
    assert chart_for(pa.table({"a": ["x"], "b": [1], "c": [2]})) is None
    assert (
        chart_for(pa.table({"a": pa.array([], pa.string()), "b": pa.array([], pa.int64())})) is None
    )
    many = pa.table({"artist": [str(i) for i in range(60)], "n": list(range(60))})
    assert chart_for(many) is None


def test_rows_csv_formats_values() -> None:
    table = pa.table({"day": [date(2025, 1, 1)], "name": ["Ana, Pop"], "missing": [None]})
    assert rows_csv(table) == 'day,name,missing\n2025-01-01,"Ana, Pop",\n'


def test_result_json_hides_usage_for_local_providers(demo_store: Store, home: Path) -> None:
    stub = StubProvider(replies=[COUNT])
    data = asker(demo_store, stub).ask(QUESTION).to_json()
    assert data["usage"] is None
    assert data["columns"] == ["n"]
    assert data["row_count"] == 1
    assert data["rows"][0]["n"] == demo_store.table_counts()["messages"]


def test_csv_caps_huge_cells_rows_and_utf8_bytes() -> None:
    import csv
    import io

    table = pa.table({f"column{i}": ['é"\n' * 10000] * 60 for i in range(20)})
    text = rows_csv(table)
    parsed = list(csv.reader(io.StringIO(text)))
    assert len(text.encode()) <= 8192
    assert 0 < len(parsed) - 1 <= 50
    assert all(len(cell) <= 200 and cell.endswith("…") for row in parsed[1:] for cell in row)


def test_whole_history_aggregate_is_truncated_before_answer(demo_store: Store, home: Path) -> None:
    import csv
    import io

    stub = StubProvider(
        replies=[
            json.dumps({"sql": "SELECT string_agg(text, ' ') AS history FROM messages"}),
            "Synthetic answer",
        ]
    )
    stub.remote = True
    Asker(demo_store, LLM(stub)).ask("Summarize all messages")
    assert "rows as CSV:" not in stub.calls[0][1]["content"]
    answer = stub.calls[-1][1]["content"].split("as CSV:\n", 1)[1]
    rows = list(csv.reader(io.StringIO(answer)))
    assert len(answer.encode()) <= 8192
    assert len(rows) == 2
    assert len(rows[1][0]) == 200
    assert rows[1][0].endswith("…")
