"""The golden-question suite (PLAN §7): canned plans through the full pipeline on the demo DB."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from sherd_agent.ask import Asker
from sherd_agent.llm import LLM
from sherd_agent.providers.stub import StubProvider
from sherd_core import Store

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden_questions.yaml").read_text())


def test_suite_size_and_mix() -> None:
    kinds = [case["expect"]["kind"] for case in GOLDEN]
    assert len(GOLDEN) >= 20
    assert kinds.count("sql") >= 10
    assert kinds.count("dig") >= 5
    assert "refuse" in kinds
    assert len({case["question"] for case in GOLDEN}) == len(GOLDEN)


@pytest.mark.parametrize("case", GOLDEN, ids=[case["question"] for case in GOLDEN])
def test_golden_question(case: dict[str, Any], demo_store: Store, home: Path) -> None:
    stub = StubProvider({case["question"]: case["plan"]})
    result = Asker(demo_store, LLM(stub), tz="Europe/Bucharest").ask(case["question"])
    expect = case["expect"]

    assert result.kind == expect["kind"]
    if result.kind == "sql":
        assert result.sql == case["plan"]["sql"].strip()
    if result.kind == "dig":
        assert result.dig == case["plan"]["dig"]
        assert result.params is not None
        assert {k: result.params[k] for k in case["plan"].get("params", {})} == case["plan"].get(
            "params", {}
        )
    if "columns" in expect:
        assert result.columns == expect["columns"]
    for column in expect.get("columns_include", []):
        assert column in result.columns
    if "rows" in expect:
        assert result.row_count == expect["rows"]
    assert expect.get("rows_min", 0) <= result.row_count <= expect.get("rows_max", 10_000)
    if "first" in expect:
        assert result.table is not None
        first = result.table.slice(0, 1).to_pylist()[0]
        assert {k: first[k] for k in expect["first"]} == expect["first"]
    if "chart" in expect:
        assert (result.chart is not None) == expect["chart"]
    assert not result.truncated
    # One planning call; a second call writes the answer unless the plan was a refusal.
    assert len(stub.calls) == (1 if result.kind == "refuse" else 2)
