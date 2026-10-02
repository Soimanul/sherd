"""Exact cross-source values, local calendars, missing streams and performance."""

import json
import os
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import pytest
from sherd_connectors.synth import import_profile
from sherd_core import Event, MediaPlay, Message, Row, Store, Transaction
from sherd_insights import DigParams, DigResult, charts
from sherd_insights.digs.cross_metrics import DIGS
from sherd_insights.registry import discover

IDS = [dig.id for dig in DIGS] + ["trends.yoy"]


def play(key: str, ts: str, *, minutes: int = 1, track: str = "Track A", **extra: Any) -> MediaPlay:
    return MediaPlay(
        source_file="synthetic.json",
        source_row_id=key,
        ts=datetime.fromisoformat(ts),
        media_kind="track",
        artist="Artist A",
        track=track,
        ms_played=minutes * 60000,
        **extra,
    )


def event(key: str, ts: str, *, kind: str = "youtube.watch") -> Event:
    return Event(
        source_file="synthetic.json", source_row_id=key, ts=datetime.fromisoformat(ts), kind=kind
    )


def message(
    key: str, ts: str, *, me: bool = True, kind: Literal["text", "system"] = "text"
) -> Message:
    return Message(
        source_file="synthetic.txt",
        source_row_id=key,
        ts=datetime.fromisoformat(ts),
        chat_id="synthetic",
        chat_kind="direct",
        is_from_me=me,
        kind=kind,
    )


def transaction(
    key: str,
    ts: str,
    amount: str,
    *,
    currency: str = "RON",
    kind: Literal["card", "refund", "exchange", "topup"] = "card",
) -> Transaction:
    return Transaction(
        source_file="synthetic.csv",
        source_row_id=key,
        ts=datetime.fromisoformat(ts),
        amount=amount,
        currency=currency,
        account="synthetic",
        kind=kind,
    )


def insert(store: Store, rows: list[Row]) -> None:
    key = store.begin_import("synthetic", "1", "synthetic", "UTC")
    store.upsert(key, "synthetic", rows)
    store.finish_import(key, "succeeded")


def compute(store: Store, metric: str, params: DigParams | None = None) -> DigResult:
    return discover()["cross." + metric].compute(store, params or DigParams())


def test_timeline_exact_streams_dst_and_busiest_day(store: Store) -> None:
    insert(
        store,
        [
            message("m0", "2024-03-30T21:59:00+00:00"),
            message("m1", "2024-03-30T22:00:00+00:00"),
            message("m2", "2024-03-31T01:30:00+00:00"),
            play("p1", "2024-03-31T20:59:00+00:00"),
            play("p2", "2024-03-31T21:00:00+00:00"),
            transaction("t", "2024-03-31T12:00:00+00:00", "-2"),
            event("e1", "2024-03-31T12:00:00+00:00"),
            event("e2", "2024-03-31T12:01:00+00:00", kind="google.search"),
        ],
    )
    got = compute(
        store,
        "life_timeline",
        DigParams(date(2024, 3, 31), date(2024, 3, 31), granularity="day", tz="Europe/Bucharest"),
    )
    assert [(r["stream"], r["activity"]) for r in got.data.to_pylist()] == [
        ("google", 1),
        ("messages", 2),
        ("plays", 1),
        ("transactions", 1),
        ("youtube", 1),
    ]
    assert got.headline
    assert got.headline.value == 6
    assert "2024-03-31" in got.headline.label
    buckets: list[tuple[Literal["week", "month"], date, date]] = [
        ("week", date(2024, 3, 25), date(2024, 4, 1)),
        ("month", date(2024, 3, 1), date(2024, 4, 1)),
    ]
    for granularity, bucket, end in buckets:
        result = compute(
            store,
            "life_timeline",
            DigParams(
                date(2024, 3, 31), date(2024, 3, 31), granularity=granularity, tz="Europe/Bucharest"
            ),
        )
        assert set(result.data["bucket"].to_pylist()) == {bucket}
        assert set(result.data["bucket_end"].to_pylist()) == {end}
        assert result.headline
        assert result.headline.value == 6


def test_timeline_missing_tables_and_eight_stream_cap() -> None:
    # Minimal hand-built DB deliberately omits canonical tables. Production writes remain in Store.
    conn = duckdb.connect()
    conn.execute("CREATE TABLE events(ts TIMESTAMPTZ, kind VARCHAR)")
    conn.execute("INSERT INTO events VALUES (?, ?)", ["2024-01-01T00:00:00Z", "youtube.watch"])
    for i in range(10):
        conn.execute(
            "INSERT INTO events VALUES (?, ?)", ["2024-01-01T00:00:00Z", f"synthetic{i}.event"]
        )
    with Store(conn, read_only=True) as store:
        got = compute(store, "life_timeline")
        assert got.data.num_rows == 11
        assert got.headline
        assert got.headline.value == 11
        assert got.chart
        assert len({r["stream"] for r in got.chart["data"]["values"]}) == 8
        assert "8 of 11" in got.text_summary


def test_soundtrack_exact_ties_defaults_range_and_dst(store: Store) -> None:
    insert(
        store,
        [
            play("old", "2024-03-01T12:00:00+00:00", minutes=50, track="Old"),
            play("a1", "2024-03-31T00:00:00+00:00", minutes=1),
            play("a2", "2024-03-31T01:30:00+00:00", minutes=2),
            play("b1", "2024-03-31T20:59:00+00:00", minutes=5, track="Track B"),
            play("b2", "2024-03-31T21:00:00+00:00", minutes=5, track="Track B"),
            play("video", "2024-04-02T12:00:00+00:00", track="Excluded"),
        ],
    )
    got = compute(
        store, "soundtrack", DigParams(date(2024, 3, 31), date(2024, 3, 31), tz="Europe/Bucharest")
    )
    assert got.data.to_pylist() == [
        {
            "artist": "Artist A",
            "track": "Track A",
            "label": "Track A · Artist A",
            "plays": 2,
            "minutes": 3.0,
        },
        {
            "artist": "Artist A",
            "track": "Track B",
            "label": "Track B · Artist A",
            "plays": 1,
            "minutes": 5.0,
        },
    ]
    default = compute(store, "soundtrack", DigParams(tz="Europe/Bucharest", top_n=1))
    assert default.data.to_pylist()[0]["track"] == "Track B"
    assert default.headline
    assert default.headline.value == "Track B · Artist A"
    assert "2024-03-04 to 2024-04-02" in default.narrative
    assert got.chart
    assert got.chart["encoding"]["y"]["field"] == "label"


def test_spend_listening_exact_currency_refunds_zero_week_and_dst(store: Store) -> None:
    insert(
        store,
        [
            transaction("t1", "2024-03-24T22:00:00+00:00", "-10"),
            transaction("t2", "2024-03-31T21:00:00+00:00", "-40"),
            transaction("refund", "2024-04-02T12:00:00+00:00", "10", kind="refund"),
            transaction("t3", "2024-04-08T12:00:00+00:00", "-20"),
            transaction("exchange", "2024-04-08T12:00:00+00:00", "-100", kind="exchange"),
            transaction("topup", "2024-04-08T12:00:00+00:00", "-100", kind="topup"),
            transaction("euro", "2024-04-08T12:00:00+00:00", "-999", currency="EUR"),
            play("p1", "2024-03-31T20:59:00+00:00", minutes=2),
            play("p3", "2024-04-08T12:00:00+00:00", minutes=4),
        ],
    )
    got = compute(store, "spend_vs_listening", DigParams(tz="Europe/Bucharest"))
    rows = got.data.to_pylist()
    assert [(r["week"], r["spend"], r["minutes"]) for r in rows] == [
        (date(2024, 3, 25), 10.0, 2.0),
        (date(2024, 4, 1), 30.0, 0.0),
        (date(2024, 4, 8), 20.0, 4.0),
    ]
    assert rows[0]["r"] == pytest.approx(-0.5)
    assert {r["n_weeks"] for r in rows} == {3}
    assert got.headline
    assert got.headline.value == pytest.approx(-0.5)
    assert "correlation, not a cause" in got.narrative
    assert "Amounts are in RON" in got.narrative
    euro = compute(store, "spend_vs_listening", DigParams(currency="EUR", tz="Europe/Bucharest"))
    assert sum(float(r["spend"]) for r in euro.data.to_pylist()) == 999


def test_messages_youtube_exact_weak_relationship_sent_only_and_dst(store: Store) -> None:
    rows: list[Row] = []
    for week, (sent, watched) in enumerate([(1, 1), (2, 3), (3, 3), (4, 1)]):
        ts = (
            datetime.fromisoformat("2024-03-24T22:00:00+00:00") + timedelta(weeks=week)
        ).isoformat()
        # After DST, 22:00 UTC still belongs to Monday in Bucharest.
        rows.extend(message(f"m{week}-{i}", ts) for i in range(sent))
        rows.extend(event(f"e{week}-{i}", ts) for i in range(watched))
    rows += [
        message("incoming", "2024-03-25T12:00:00+00:00", me=False),
        message("system", "2024-03-25T12:00:00+00:00", kind="system"),
        event("search", "2024-03-25T12:00:00+00:00", kind="google.search"),
    ]
    insert(store, rows)
    got = compute(store, "messages_vs_youtube", DigParams(tz="Europe/Bucharest"))
    assert [(r["messages"], r["youtube_watches"]) for r in got.data.to_pylist()] == [
        (1, 1),
        (2, 3),
        (3, 3),
        (4, 1),
    ]
    assert got.headline
    assert got.headline.value == 0
    assert "4 weeks" in got.headline.label
    assert "no clear relationship" in got.narrative
    assert "correlation, not a cause" in got.narrative


def test_correlation_constant_and_single_week_no_nan(store: Store) -> None:
    insert(
        store, [message("m", "2024-01-01T12:00:00+00:00"), event("e", "2024-01-01T12:00:00+00:00")]
    )
    got = compute(store, "messages_vs_youtube")
    assert got.data["r"].to_pylist() == [None]
    assert "no clear relationship" in got.narrative
    assert got.chart
    assert len(got.chart["layer"]) == 1
    json.dumps(got.chart, allow_nan=False)


@pytest.mark.parametrize("dig_id", IDS)
def test_empty_database(store: Store, dig_id: str) -> None:
    got = discover()[dig_id].compute(store, DigParams())
    assert got.data.num_rows == 0
    assert got.chart is None
    assert got.headline is None
    assert got.narrative
    assert got.text_summary


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    with Store.open(tmp_path_factory.mktemp("cross-demo") / "demo.duckdb") as store:
        import_profile(store, "demo")
        yield store


@pytest.mark.parametrize("dig_id", IDS)
def test_demo_nonempty_deterministic_under_one_second(demo: Store, dig_id: str) -> None:
    dig = discover()[dig_id]
    params = DigParams(tz="Europe/Bucharest")
    start = time.perf_counter()
    result = dig.compute(demo, params)
    elapsed = time.perf_counter() - start
    assert elapsed < 1, (dig_id, elapsed)
    assert result.data.num_rows
    assert result.chart
    assert result.headline
    assert result.chart["config"] == charts.load_theme()
    assert result.text_summary
    assert result.narrative
    json.dumps(result.chart, allow_nan=False)
    again = dig.compute(demo, params)
    assert result.data.equals(again.data)
    assert result.chart == again.chart


def test_timeline_200k_under_point_two_seconds(tmp_path: Path) -> None:
    with Store.open(tmp_path / "large.duckdb") as store:
        key = store.begin_import("synthetic", "1", "synthetic", "UTC")
        # Stream synthetic canonical rows; setup is outside the measured compute.
        store.upsert(
            key,
            "synthetic",
            (
                message(
                    str(i),
                    (
                        datetime.fromisoformat("2024-01-01T00:00:00+00:00") + timedelta(minutes=i)
                    ).isoformat(),
                )
                for i in range(200_000)
            ),
        )
        store.finish_import(key, "succeeded")
        start = time.perf_counter()
        got = compute(store, "life_timeline", DigParams(tz="Europe/Bucharest"))
        elapsed = time.perf_counter() - start
        assert got.data.num_rows
        assert sum(int(r["activity"]) for r in got.data.to_pylist()) == 200_000
        assert elapsed < 0.2, elapsed


@pytest.mark.skipif(
    os.environ.get("SHERD_BENCH") != "1", reason="set SHERD_BENCH=1 for 2M-row profile"
)
def test_timeline_bench_under_one_second(tmp_path: Path) -> None:
    with Store.open(tmp_path / "bench.duckdb") as store:
        import_profile(store, "bench")
        start = time.perf_counter()
        got = compute(store, "life_timeline", DigParams(tz="Europe/Bucharest"))
        elapsed = time.perf_counter() - start
        print(f"life_timeline bench: {elapsed:.6f}s")
        assert sum(int(r["activity"]) for r in got.data.to_pylist()) == sum(
            store.table_counts().values()
        )
        assert elapsed < 1, elapsed


def test_spend_listening_weak_relationship_inclusive_range(store: Store) -> None:
    rows: list[Row] = []
    for week, (spend, minutes) in enumerate([(1, 1), (2, 3), (3, 3), (4, 1)]):
        ts = (
            datetime.fromisoformat("2024-01-01T12:00:00+00:00") + timedelta(weeks=week)
        ).isoformat()
        rows += [transaction(f"t{week}", ts, str(-spend)), play(f"p{week}", ts, minutes=minutes)]
    rows += [
        transaction("excluded", "2023-12-25T12:00:00+00:00", "-100"),
        play("excluded", "2024-02-01T12:00:00+00:00", minutes=100),
    ]
    insert(store, rows)
    got = compute(store, "spend_vs_listening", DigParams(date(2024, 1, 1), date(2024, 1, 22)))
    assert [(r["spend"], r["minutes"]) for r in got.data.to_pylist()] == [
        (1, 1),
        (2, 3),
        (3, 3),
        (4, 1),
    ]
    assert got.headline
    assert got.headline.value == 0
    assert "no clear relationship" in got.narrative
    assert "correlation, not a cause" in got.narrative


def test_messages_youtube_zero_week_and_autumn_dst(store: Store) -> None:
    insert(
        store,
        [
            message("m1", "2024-10-27T21:59:00+00:00"),  # Sunday local, after DST
            message("m2", "2024-10-27T22:00:00+00:00"),  # Monday local
            event("y1", "2024-10-27T00:30:00+00:00"),  # first 03:30
            event("y2", "2024-10-27T01:30:00+00:00"),
        ],
    )  # second 03:30
    got = compute(
        store,
        "messages_vs_youtube",
        DigParams(date(2024, 10, 27), date(2024, 10, 28), tz="Europe/Bucharest"),
    )
    assert [(r["week"], r["messages"], r["youtube_watches"]) for r in got.data.to_pylist()] == [
        (date(2024, 10, 21), 1.0, 2.0),
        (date(2024, 10, 28), 1.0, 0.0),
    ]
    timeline = compute(
        store,
        "life_timeline",
        DigParams(date(2024, 10, 27), date(2024, 10, 27), granularity="day", tz="Europe/Bucharest"),
    )
    assert timeline.headline
    assert timeline.headline.value == 3


@pytest.mark.parametrize("metric", ["soundtrack", "spend_vs_listening", "messages_vs_youtube"])
def test_empty_selected_range_and_missing_comparison_stream(store: Store, metric: str) -> None:
    insert(
        store, [play("p", "2024-01-01T12:00:00+00:00"), message("m", "2024-01-01T12:00:00+00:00")]
    )
    got = compute(store, metric, DigParams(date_from=date(2099, 1, 1)))
    assert got.data.num_rows == 0
    assert got.chart is None
    assert got.headline is None
    if metric != "soundtrack":
        missing = compute(store, metric)
        assert missing.data.num_rows == 0
