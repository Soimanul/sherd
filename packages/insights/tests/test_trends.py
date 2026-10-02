"""Exact YoY ranges, deltas, currency consistency, and optional dig skip rules."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pyarrow as pa
import pytest
from sherd_core import Store
from sherd_insights import DigParams, DigResult, Headline
from sherd_insights.base import Dig
from sherd_insights.digs import trends
from sherd_insights.registry import discover

from .test_cross import insert, message, play, transaction


def test_yoy_two_real_digs_exact_deltas_local_year_and_no_recursion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(
        store,
        [
            play("a", "2023-06-01T12:00:00+00:00", minutes=120),
            play("b", "2024-06-01T12:00:00+00:00", minutes=60),
            message("m1", "2023-06-01T12:00:00+00:00"),
            message("m2", "2023-06-01T12:01:00+00:00"),
            message("m3", "2024-06-01T12:00:00+00:00"),
            message("cutoff", "2024-12-30T22:30:00+00:00"),  # Dec 31 locally: full 2024
        ],
    )
    registered = discover()
    digs = [registered[k] for k in ("cross.life_timeline", "music.listening_minutes", "trends.yoy")]
    monkeypatch.setattr(trends, "available", lambda store: digs)
    got = trends.YearOverYear().compute(store, DigParams(tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [
        {
            "dig_id": "music.listening_minutes",
            "label": "Listening time",
            "dig_title": "Listening time",
            "delta_unit": "%",
            "skipped_digs": 1,
            "unit": "hours",
            "this_year": 1.0,
            "last_year": 2.0,
            "delta_pct": -50.0,
            "year": 2024,
            "previous_year": 2023,
        },
    ]
    assert got.headline
    assert got.headline.label == "Listening time"
    assert got.headline.value == -50
    assert got.chart
    assert got.chart["encoding"]["y"]["field"] == "label"
    assert len(got.chart["layer"]) == 2


@dataclass
class StubDig:
    id: str
    mode: Literal["numeric", "error", "empty", "string", "missing", "zero", "nan", "unit"]
    title: str = "Synthetic headline"
    requires: list[str] = field(default_factory=list)
    unit: str = "count"
    label: str = "Synthetic"
    calls: list[DigParams] = field(default_factory=list)

    def compute(self, store: Store, params: DigParams) -> DigResult:
        self.calls.append(params)
        assert params.date_from
        assert params.date_to
        assert params.date_from.month == 1
        assert params.date_from.day == 1
        assert params.date_to.month == 12
        assert params.date_to.day == 31
        if self.mode == "error":
            raise ValueError("synthetic failure")
        value: int | float | str = 20 if params.date_from.year == 2024 else 10
        if self.mode == "zero" and params.date_from.year == 2023:
            value = 0
        if self.mode == "string":
            value = "Synthetic label"
        if self.mode == "nan":
            value = float("nan")
        headline = Headline(self.label, value, self.unit)
        if self.mode == "unit" and params.date_from.year == 2023:
            headline = Headline("Synthetic", value, "other")
        return DigResult(
            pa.table({"n": [] if self.mode == "empty" else [1]}),
            None,
            "Synthetic narrative",
            None if self.mode == "missing" else headline,
            "Synthetic summary",
        )


def test_yoy_skip_rules_zero_baselines_and_currency_locked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(
        store,
        [
            transaction("old", "2023-06-01T12:00:00+00:00", "-10", currency="EUR"),
            transaction("new", "2024-06-01T12:00:00+00:00", "-20"),
            transaction("extra", "2024-06-02T12:00:00+00:00", "-20"),
            message("cutoff", "2025-06-01T12:00:00+00:00"),
        ],
    )
    modes: list[
        Literal["numeric", "error", "empty", "string", "missing", "zero", "nan", "unit"]
    ] = ["numeric", "error", "empty", "string", "missing", "zero", "nan", "unit"]
    stubs = [StubDig("synthetic." + mode, mode) for mode in modes]
    digs: list[Dig] = [*stubs, trends.YearOverYear()]
    monkeypatch.setattr(trends, "available", lambda store: digs)
    result = trends.YearOverYear().compute(store, DigParams())
    assert [(r["dig_id"], r["delta_pct"]) for r in result.data.to_pylist()] == [
        ("synthetic.numeric", 100.0),
        ("synthetic.zero", None),
    ]
    assert result.chart
    assert len(result.chart["data"]["values"]) == 1
    assert all(call.currency == "RON" for stub in stubs for call in stub.calls)
    assert all(
        call.date_from and call.date_from.year in (2023, 2024)
        for stub in stubs
        for call in stub.calls
    )


def test_yoy_date_to_cutoff_explicit_currency_and_zero_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(store, [message("latest", "2026-01-01T12:00:00+00:00")])
    stub = StubDig("synthetic.zero", "zero")
    monkeypatch.setattr(trends, "available", lambda store: [stub])
    result = trends.YearOverYear().compute(
        store, DigParams(date_to=date(2024, 12, 31), currency="EUR")
    )
    assert result.data.to_pylist()[0]["year"] == 2024
    assert result.data.to_pylist()[0]["delta_pct"] is None
    assert result.chart is None
    assert result.headline is None
    assert all(call.currency == "EUR" for call in stub.calls)
    stub.calls.clear()
    trends.YearOverYear().compute(store, DigParams(date_to=date(2024, 12, 30)))
    assert stub.calls[0].date_from == date(2023, 1, 1)


def test_yoy_empty_when_all_candidates_fail(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    insert(store, [message("latest", "2025-01-01T00:00:00+00:00")])
    monkeypatch.setattr(trends, "available", lambda store: [StubDig("synthetic.error", "error")])
    result = trends.YearOverYear().compute(store, DigParams())
    assert result.data.num_rows == 0
    assert result.chart is None
    assert result.headline is None


def test_yoy_percentage_points_quantity_labels_and_correlation_exclusion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(store, [message("latest", "2025-01-01T00:00:00+00:00")])
    rate = StubDig("synthetic.rate", "zero", unit="%", label="Discovery share")
    quantity = StubDig("synthetic.quantity", "numeric", label="Listening hours")
    correlations = [
        StubDig(id, "numeric", unit="r")
        for id in ("cross.spend_vs_listening", "cross.messages_vs_youtube", "synthetic.correlation")
    ]
    monkeypatch.setattr(trends, "available", lambda store: [rate, quantity, *correlations])
    result = trends.YearOverYear().compute(store, DigParams())
    rows = result.data.to_pylist()
    assert [(r["label"], r["delta_pct"], r["delta_unit"]) for r in rows] == [
        ("Listening hours", 100, "%"),
        ("Discovery share", 20, "pp"),
    ]
    assert all(r["dig_title"] == "Synthetic headline" and r["skipped_digs"] == 3 for r in rows)
    assert result.headline
    assert result.headline.label == "Listening hours"
    monkeypatch.setattr(trends, "available", lambda store: [rate])
    result = trends.YearOverYear().compute(store, DigParams())
    assert result.headline
    assert result.headline.unit == "pp"
    assert "+20.0 pp" in result.narrative


def test_yoy_logs_only_dig_id_exception_class_and_counts_empty_skips(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    insert(store, [message("latest", "2025-01-01T00:00:00+00:00")])
    monkeypatch.setattr(trends, "available", lambda store: [StubDig("synthetic.error", "error")])
    result = trends.YearOverYear().compute(store, DigParams())
    assert "Skipped dig synthetic.error (ValueError)" in caplog.text
    assert "synthetic failure" not in caplog.text
    assert result.data.schema.metadata == {b"skipped_digs": b"1"}
    assert "Skipped 1 digs" in result.narrative
