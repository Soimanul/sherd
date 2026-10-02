from datetime import date

import pytest
from sherd_core import Store
from sherd_insights import DigParams, DigResult
from sherd_insights.registry import discover

from .conftest import insert, message


def compute(store: Store, metric: str, params: DigParams | None = None) -> DigResult:
    return discover()["messages." + metric].compute(store, params or DigParams())


def test_volume_both_directions_top_contact_and_inclusive_local_range(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-01-31T22:30:00+00:00", contact_id="resolved"),
            message("2", "2024-02-01T00:30:00+00:00", me=False, contact_id="resolved"),
            message("3", "2024-02-01T09:00:00+00:00", contact="Contact B"),
            message("4", "2024-02-01T09:00:00+00:00", kind="system"),
            message("5", "2024-02-01T09:00:00+00:00", chat_kind="group"),
            message("6", "2024-02-02T00:00:00+00:00"),
        ],
    )
    got = compute(
        store,
        "volume_by_contact",
        DigParams(date(2024, 2, 1), date(2024, 2, 1), 1, "day", "Europe/Bucharest"),
    )
    assert got.data.to_pylist() == [
        {"contact": "resolved", "bucket": date(2024, 2, 1), "messages": 2}
    ]
    assert got.headline
    assert (got.headline.label, got.headline.value) == ("resolved", 2)


def test_yearly_ranks_and_rise(store: Store) -> None:
    rows = []
    for year, counts in (
        (2023, {"Contact A": 3, "Contact B": 1}),
        (2024, {"Contact A": 1, "Contact B": 3}),
    ):
        for contact, count in counts.items():
            for i in range(count):
                rows.append(
                    message(
                        f"{year}-{contact}-{i}", f"{year}-01-01T00:0{i}:00+00:00", contact=contact
                    )
                )
    insert(store, rows)
    got = compute(store, "top_contacts_by_year", DigParams(top_n=2))
    assert got.data.to_pylist() == [
        {"contact": "Contact A", "year": 2023, "messages": 3, "rank": 1},
        {"contact": "Contact B", "year": 2023, "messages": 1, "rank": 2},
        {"contact": "Contact B", "year": 2024, "messages": 3, "rank": 1},
        {"contact": "Contact A", "year": 2024, "messages": 1, "rank": 2},
    ]
    assert got.headline
    assert got.headline.value == "Contact B"
    assert got.headline.delta is None
    assert "rose 1 places" in got.narrative

    top_one = compute(store, "top_contacts_by_year", DigParams(top_n=1))
    assert top_one.data.num_rows == 2
    assert top_one.headline is not None
    assert top_one.headline.value == "Contact B"
    assert "rose 1 places" in top_one.narrative


def test_reply_first_after_last_and_24_hour_cap(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-01-01T00:00:00+00:00", me=False),
            message("2", "2024-01-01T00:10:00+00:00", me=False),
            message("3", "2024-01-01T00:30:00+00:00"),
            message("4", "2024-01-01T00:35:00+00:00"),
            message("5", "2024-01-02T00:35:00+00:00", me=False),
            message("6", "2024-01-03T00:35:01+00:00"),
            message("7", "2024-01-03T00:45:01+00:00", me=False),
        ],
    )
    got = compute(store, "response_times")
    assert got.data.to_pylist() == [
        {"contact": "Contact A", "side": "them", "minutes": 725.0, "replies": 2},
        {"contact": "Contact A", "side": "you", "minutes": 20.0, "replies": 1},
    ]
    assert got.headline
    assert got.headline.value == 20


def test_conversation_gap_at_six_hours_and_overall_before_top_limit(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-01-01T00:00:00+00:00"),
            message("2", "2024-01-01T05:59:59+00:00", me=False),
            message("3", "2024-01-01T11:59:59+00:00", me=False),
            message("4", "2024-01-02T00:00:00+00:00", contact="Contact B"),
        ],
    )
    got = compute(store, "conversation_starters", DigParams(top_n=1))
    assert got.data.to_pylist() == [
        {
            "contact": "Contact A",
            "conversations": 2,
            "started_by_you": 1,
            "your_share": 0.5,
            "overall_share": 2 / 3,
        }
    ]
    assert got.headline
    assert got.headline.value == 66.7


def test_heatmap_dst_spring_and_fall_including_group_messages(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-03-31T00:30:00+00:00"),  # 02:30 before spring jump
            message("2", "2024-03-31T01:30:00+00:00", chat_kind="group"),  # 04:30
            message("3", "2024-03-31T02:30:00+00:00"),  # 05:30
            message("4", "2024-03-31T00:30:00+00:00", me=False),
            message("5", "2024-10-27T00:30:00+00:00"),  # repeated 03:30
            message("6", "2024-10-27T01:30:00+00:00"),
        ],
    )
    got = compute(store, "activity_heatmap", DigParams(tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [
        {"hour": 2, "weekday": 7, "messages": 1},
        {"hour": 3, "weekday": 7, "messages": 2},
        {"hour": 4, "weekday": 7, "messages": 1},
        {"hour": 5, "weekday": 7, "messages": 1},
    ]
    assert got.headline
    assert got.headline.value == 80


def test_streaks_use_distinct_local_dates_and_calendar_gaps(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-03-30T21:30:00+00:00"),  # March 30 local
            message("2", "2024-03-30T22:30:00+00:00"),  # March 31 local
            message("3", "2024-03-31T21:30:00+00:00", me=False),  # April 1 local
            message("4", "2024-03-31T22:30:00+00:00"),  # same local date
            message("5", "2024-04-05T22:30:00+00:00"),  # April 6 local
        ],
    )
    got = compute(store, "streaks_silences", DigParams(tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [{"contact": "Contact A", "streak_days": 3, "silence_days": 5}]
    assert got.headline
    assert got.headline.value == 3


def test_emoji_zwj_skin_tones_words_urls_multilingual_stopwords(store: Store) -> None:
    insert(
        store,
        [
            message(
                "1",
                "2024-01-01T00:00:00+00:00",
                text="the and pentru este Café CAFÉ 👩🏽‍💻 👩🏽‍💻 😀 https://example.com/ignored",
            ),
            message(
                "2", "2024-01-01T01:00:00+00:00", text="der und pădure pădure 😀", chat_kind="group"
            ),
            message("3", "2024-01-01T02:00:00+00:00", me=False, text="unwanted 😀"),
            message("4", "2024-01-01T03:00:00+00:00", kind="media", text="unwanted 😀"),
        ],
    )
    got = compute(store, "emoji_words", DigParams(top_n=2))
    assert got.data.to_pylist() == [
        {"year": 2024, "kind": "emoji", "token": "👩🏽‍💻", "count": 2},
        {"year": 2024, "kind": "emoji", "token": "😀", "count": 2},
        {"year": 2024, "kind": "word", "token": "café", "count": 2},
        {"year": 2024, "kind": "word", "token": "pădure", "count": 2},
    ]
    assert got.headline
    assert got.headline.value == "👩🏽‍💻"


@pytest.mark.parametrize("metric", ["response_times", "conversation_starters"])
def test_sources_do_not_share_conversations(store: Store, metric: str) -> None:
    insert(store, [message("1", "2024-01-01T00:00:00+00:00", me=False)], source="first")
    insert(store, [message("2", "2024-01-01T00:01:00+00:00")], source="second")
    got = compute(store, metric)
    if metric == "response_times":
        assert got.data.num_rows == 0
        assert got.chart is None
    else:
        assert got.data.to_pylist()[0]["conversations"] == 2


def test_words_without_emoji_and_no_own_replies(store: Store) -> None:
    insert(
        store,
        [
            message("1", "2024-01-01T00:00:00+00:00", text="synthetic vocabulary"),
            message("2", "2024-01-01T00:01:00+00:00", me=False),
        ],
    )
    assert compute(store, "emoji_words").data.num_rows == 2
    got = compute(store, "response_times")
    assert got.headline
    assert got.headline.value == "No replies"
