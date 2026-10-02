"""Exact music contracts and demo/empty/performance checks."""

import json
import time
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Literal

import pytest
from sherd_connectors.synth import import_profile
from sherd_core import MediaPlay, Store
from sherd_insights import DigParams, DigResult, charts
from sherd_insights.registry import discover

IDS = [
    "music." + metric
    for metric in (
        "listening_minutes",
        "top_artists_by_year",
        "discovery_rate",
        "seasonality",
        "skips_obsessions",
    )
]


def play(
    key: str, ts: str, *, artist: str | None = "Artist A", minutes: int = 1, **extra: Any
) -> MediaPlay:
    fields: dict[str, Any] = {
        "source_file": "synthetic.json",
        "source_row_id": key,
        "ts": datetime.fromisoformat(ts),
        "media_kind": "track",
        "artist": artist,
        "track": "Track A",
        "ms_played": minutes * 60000,
    }
    fields.update(extra)
    return MediaPlay(**fields)


def insert(store: Store, rows: list[MediaPlay]) -> None:
    key = store.begin_import("synthetic", "1", "synthetic", "UTC")
    store.upsert(key, "synthetic", rows)
    store.finish_import(key, "succeeded")


def compute(store: Store, metric: str, params: DigParams | None = None) -> DigResult:
    return discover()["music." + metric].compute(store, params or DigParams())


def test_listening_local_inclusive_range_and_media_kinds(store: Store) -> None:
    insert(
        store,
        [
            play("1", "2024-01-31T22:30:00+00:00", minutes=60),
            play("2", "2024-02-01T00:30:00+00:00", minutes=30, media_kind="episode"),
            play("3", "2024-02-01T01:00:00+00:00", minutes=500, media_kind="video"),
            play("4", "2024-02-01T22:00:00+00:00", minutes=100),
            play("5", "2024-02-01T02:00:00+00:00", minutes=500, media_kind="audiobook"),
        ],
    )
    got = compute(
        store,
        "listening_minutes",
        DigParams(date(2024, 2, 1), date(2024, 2, 1), granularity="day", tz="Europe/Bucharest"),
    )
    assert got.data.to_pylist() == [{"bucket": date(2024, 2, 1), "minutes": 90.0}]
    assert got.headline
    assert got.headline.value == 1.5
    buckets: list[tuple[Literal["day", "week", "month", "year"], date]] = [
        ("week", date(2024, 1, 29)),
        ("month", date(2024, 2, 1)),
        ("year", date(2024, 1, 1)),
    ]
    for granularity, bucket in buckets:
        result = compute(
            store,
            "listening_minutes",
            DigParams(
                date(2024, 2, 1), date(2024, 2, 1), granularity=granularity, tz="Europe/Bucharest"
            ),
        )
        assert result.data.to_pylist() == [{"bucket": bucket, "minutes": 90.0}]


def test_top_artists_latest_local_year_tracks_and_limit(store: Store) -> None:
    insert(
        store,
        [
            play("old", "2023-01-01T12:00:00+00:00", minutes=50),
            play(
                "new", "2023-12-31T23:00:00+00:00", artist="Artist B", minutes=10, track="Track B"
            ),
            play("tie", "2024-06-01T12:00:00+00:00", artist="Artist C", minutes=10),
            play("episode", "2024-06-01T12:00:00+00:00", minutes=100, media_kind="episode"),
        ],
    )
    got = compute(store, "top_artists_by_year", DigParams(top_n=1, tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [
        {
            "year": 2023,
            "artist": "Artist A",
            "minutes": 50.0,
            "rank": 1,
            "top_tracks": [{"artist": "Artist A", "track": "Track A", "minutes": 50.0, "rank": 1}],
        },
        {
            "year": 2024,
            "artist": "Artist B",
            "minutes": 10.0,
            "rank": 1,
            "top_tracks": [{"artist": "Artist B", "track": "Track B", "minutes": 10.0, "rank": 1}],
        },
    ]
    assert got.headline
    assert got.headline.value == "Artist B"
    assert got.chart
    assert got.chart["spec"]["encoding"]["y"]["field"] == "artist"
    insert(
        store, [play(str(i), "2024-06-02T12:00:00+00:00", artist=f"Artist {i}") for i in range(8)]
    )
    capped = compute(store, "top_artists_by_year")
    assert sum(r["year"] == 2024 for r in capped.data.to_pylist()) == 5


def test_discovery_uses_first_ever_outside_range_local_month_and_play_weight(store: Store) -> None:
    insert(
        store,
        [
            play("history", "2023-12-01T00:00:00+00:00"),
            play("old", "2024-01-05T12:00:00+00:00"),
            play("first", "2024-01-31T22:30:00+00:00", artist="Artist B"),
            play("same", "2024-02-05T12:00:00+00:00", artist="Artist B", minutes=10),
            play("old2", "2024-02-05T12:30:00+00:00"),
            play("excluded", "2023-01-01T00:00:00+00:00", artist="Artist B", media_kind="video"),
        ],
    )
    got = compute(
        store,
        "discovery_rate",
        DigParams(date(2024, 1, 1), date(2024, 2, 29), tz="Europe/Bucharest"),
    )
    assert got.data.to_pylist() == [
        {"bucket": date(2024, 1, 1), "plays": 1, "new_plays": 0, "discovery_rate": 0.0},
        {"bucket": date(2024, 2, 1), "plays": 3, "new_plays": 2, "discovery_rate": 2 / 3},
    ]
    assert got.headline
    assert got.headline.value == 33.3


def test_seasonality_exact_local_hour_edges_share_and_colours(store: Store) -> None:
    insert(
        store,
        [
            play("winter", "2024-02-29T21:59:00+00:00", minutes=3),  # winter, 23:59 night
            play("summer", "2024-05-31T22:30:00+00:00", artist="Artist B"),  # June 1 night
            play("morning", "2024-06-01T02:00:00+00:00", artist="Artist B", minutes=3),  # 05:00
            play("last", "2024-06-01T07:59:00+00:00", artist="Artist B"),  # 10:59
            play("outside", "2024-06-01T08:00:00+00:00"),  # 11:00
            play("nightlast", "2024-06-02T00:59:00+00:00", artist="Artist B"),  # 03:59
            play("notnight", "2024-06-02T01:00:00+00:00", artist="Artist B"),  # 04:00
        ],
    )
    got = compute(store, "seasonality", DigParams(tz="Europe/Bucharest", top_n=1))
    assert got.data.to_pylist() == [
        {
            "comparison": "season",
            "period": "summer",
            "artist": "Artist B",
            "minutes": 7.0,
            "share": 7 / 8,
        },
        {
            "comparison": "season",
            "period": "summer",
            "artist": "Artist A",
            "minutes": 1.0,
            "share": 1 / 8,
        },
        {
            "comparison": "season",
            "period": "winter",
            "artist": "Artist A",
            "minutes": 3.0,
            "share": 1.0,
        },
        {
            "comparison": "time",
            "period": "morning",
            "artist": "Artist B",
            "minutes": 4.0,
            "share": 1.0,
        },
        {
            "comparison": "time",
            "period": "night",
            "artist": "Artist A",
            "minutes": 3.0,
            "share": 3 / 5,
        },
        {
            "comparison": "time",
            "period": "night",
            "artist": "Artist B",
            "minutes": 2.0,
            "share": 2 / 5,
        },
    ]
    assert got.headline
    assert got.headline.value == "Artist A"
    assert got.chart
    for panel, domain in zip(
        got.chart["vconcat"], (["winter", "summer"], ["morning", "night"]), strict=True
    ):
        assert panel["encoding"]["y"]["field"] == "artist"
        assert panel["encoding"]["color"]["scale"]["domain"] == domain


def test_skips_nullable_flags_and_obsession_strict_local_iso_week(store: Store) -> None:
    rows = [play(f"thirty-{i}", "2024-01-07T21:00:00+00:00", skipped=False) for i in range(30)]
    rows += [play("nextweek", "2024-01-07T22:00:00+00:00", skipped=True)]  # Monday local
    rows += [play(f"obsessed-{i}", "2024-01-08T12:00:00+00:00", track="Track B") for i in range(31)]
    rows += [
        play(f"podcast-{i}", "2024-01-08T12:00:00+00:00", media_kind="episode") for i in range(31)
    ]
    insert(store, rows)
    got = compute(store, "skips_obsessions", DigParams(tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [
        {
            "bucket": date(2024, 1, 1),
            "known_plays": 31,
            "skips": 1,
            "skip_rate": 1 / 31,
            "obsessions": [
                {"artist": "Artist A", "track": "Track B", "week": date(2024, 1, 8), "plays": 31}
            ],
        }
    ]
    assert got.headline
    assert got.headline.value == 31
    assert "Track B" in got.headline.label
    assert "2024-01-08" in got.headline.label


def test_skips_unknown_and_empty_seasons_are_graceful(store: Store) -> None:
    insert(store, [play("1", "2024-04-01T12:00:00+00:00", artist=None)])
    got = compute(store, "skips_obsessions")
    assert got.data.to_pylist() == [
        {
            "bucket": date(2024, 4, 1),
            "known_plays": 0,
            "skips": 0,
            "skip_rate": None,
            "obsessions": [],
        }
    ]
    seasonal = compute(store, "seasonality")
    assert seasonal.chart is None
    assert seasonal.data.num_rows == 0


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    with Store.open(tmp_path_factory.mktemp("music-demo") / "demo.duckdb") as store:
        import_profile(store, "demo")
        yield store


@pytest.mark.parametrize("dig_id", IDS)
def test_demo_under_one_second_deterministic_and_empty_range(demo: Store, dig_id: str) -> None:
    dig = discover()[dig_id]
    params = DigParams(tz="Europe/Bucharest")
    started = time.perf_counter()
    got = dig.compute(demo, params)
    assert time.perf_counter() - started < 1
    assert got.data.num_rows
    assert got.chart
    assert got.headline
    assert got.narrative
    assert got.text_summary
    assert got.chart["config"] == charts.load_theme()
    json.dumps(got.chart, allow_nan=False)
    again = dig.compute(demo, params)
    assert got.data.equals(again.data)
    assert got.chart == again.chart
    assert (got.headline, got.narrative, got.text_summary) == (
        again.headline,
        again.narrative,
        again.text_summary,
    )
    empty = dig.compute(demo, DigParams(date_from=date(2099, 1, 1)))
    assert empty.data.num_rows == 0
    assert empty.chart is None
    assert empty.headline is None


@pytest.mark.parametrize("dig_id", IDS)
def test_empty_database(store: Store, dig_id: str) -> None:
    got = discover()[dig_id].compute(store, DigParams())
    assert got.data.num_rows == 0
    assert got.chart is None
    assert got.headline is None
    assert got.narrative
    assert got.text_summary


@pytest.mark.parametrize(("top_n", "limit"), [(3, 3), (10, 8), (20, 8)])
def test_seasonality_chart_artist_cap_per_side(store: Store, top_n: int, limit: int) -> None:
    insert(
        store,
        [
            play(
                f"{month}-{i}",
                f"2024-{month:02}-01T{hour}:00:00+00:00",
                artist=f"Artist {month}-{i}",
                minutes=20 - i,
            )
            for month, hour in [(1, "06"), (7, "23")]
            for i in range(12)
        ],
    )
    got = compute(store, "seasonality", DigParams(top_n=top_n))
    assert got.chart
    assert got.data.num_rows == 48
    for panel in got.chart["vconcat"]:
        values = panel["data"]["values"]
        for period in panel["encoding"]["color"]["scale"]["domain"]:
            assert len({r["artist"] for r in values if r["period"] == period}) == limit
