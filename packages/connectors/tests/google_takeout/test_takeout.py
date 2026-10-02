import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.base import ImportContext, export_root
from sherd_connectors.google_takeout import CONNECTOR, activity, records
from sherd_connectors.google_takeout.dates import MONTHS, ZONES, parse_date
from sherd_connectors.google_takeout.synth_google_takeout import GENERATOR
from sherd_connectors.pipeline import run_import
from sherd_connectors.testing import assert_streaming, export_path, load_meta
from sherd_core import Store

FIXTURES = {path.name: path for path in CONNECTOR.fixtures()}


def context(path: Path, tz: str = "Europe/Bucharest") -> ImportContext:
    return ImportContext(export_root(path), ZoneInfo(tz), frozenset())


def sample(title: str = "Watched Synthetic stars") -> dict[str, object]:
    return {
        "header": "YouTube",
        "title": title,
        "time": "2024-01-01T10:00:00Z",
        "products": ["YouTube"],
        "titleUrl": "https://example.com/watch",
    }


@pytest.mark.parametrize("variant", sorted(FIXTURES))
def test_detect_variants(variant: str) -> None:
    path = export_path(FIXTURES[variant])
    assert CONNECTOR.detect(path).confidence == 0.95
    if path.is_dir():
        assert CONNECTOR.detect(next(p for p in path.rglob("*") if p.is_file())).confidence == 0.95


def test_detect_negative(tmp_path: Path) -> None:
    assert CONNECTOR.detect(tmp_path / "absent").confidence == 0
    for data in ('[{"trackName":"Synthetic track"}]', "[]", "<p>unrelated</p>"):
        path = tmp_path / "other.json"
        path.write_text(data)
        assert CONNECTOR.detect(path).confidence == 0
    for root in FIXTURES["chrome-json"].parents[1].iterdir():
        if root.name != CONNECTOR.id:
            for meta in root.glob("*/meta.json"):
                assert CONNECTOR.detect(export_path(meta.parent)).confidence == 0


@pytest.mark.parametrize("prefix", ["Watched ", "Ați vizionat ", "Angesehen: ", "Has visto "])
def test_watch_localised_prefix(prefix: str, tmp_path: Path) -> None:
    event = activity(sample(prefix + "Synthetic stars"), context(tmp_path), False)
    assert event is not None
    assert event.title == "Synthetic stars"


def test_german_watch_suffix(tmp_path: Path) -> None:
    event = activity(sample("Synthetic stars angesehen"), context(tmp_path), False)
    assert event is not None
    assert event.title == "Synthetic stars"


@pytest.mark.parametrize("prefix", ["Searched for ", "Ați căutat ", "Gesucht nach: "])
def test_search_localised_prefix(prefix: str, tmp_path: Path) -> None:
    record = dict(sample(prefix + "Fictional planets"), header="Search", products=["Search"])
    event = activity(record, context(tmp_path), False)
    assert event is not None
    assert event.title == "Fictional planets"
    record["titleUrl"] = "https://example.com/search?q=URL+wins%21"
    event = activity(record, context(tmp_path), False)
    assert event is not None
    assert event.title == "URL wins!"


@pytest.mark.parametrize(("month", "number"), sorted(MONTHS.items()))
def test_month_table(month: str, number: int) -> None:
    assert parse_date(f"1 {month}. 2024, 12:00:00 UTC", ZoneInfo("UTC")) == datetime(
        2024, number, 1, 12, tzinfo=UTC
    )


@pytest.mark.parametrize(("zone", "offset"), sorted(ZONES.items()))
def test_timezone_table(zone: str, offset: int) -> None:
    value = parse_date(f"Jan 1, 2024, 12:00:00 PM {zone}", ZoneInfo("Asia/Tokyo"))
    assert value == datetime(2024, 1, 1, 12 - offset, tzinfo=UTC)


@pytest.mark.parametrize(
    "date",
    [
        "01.01.2024, 11:00:00 MEZ",
        "1 ene. 2024, 11:00:00 CET",
        "1 janv. 2024, 11:00:00 CET",
        "1 ian. 2024, 12:00:00 EET",
        "Jan 1, 2024, 10:00:00 AM UTC",
    ],
)
def test_localised_date(date: str) -> None:
    assert parse_date(date, ZoneInfo("UTC")) == datetime(2024, 1, 1, 10, tzinfo=UTC)


def test_fallback_and_dst() -> None:
    zone = ZoneInfo("Europe/Berlin")
    assert parse_date("31.03.2024, 02:30:00 UNKNOWN", zone) == datetime(
        2024, 3, 31, 1, 30, tzinfo=UTC
    )
    assert parse_date("27.10.2024, 02:30:00", zone) == datetime(2024, 10, 27, 0, 30, tzinfo=UTC)
    assert parse_date("Jan 1, 2024, 12:00:00 AM GMT", zone).hour == 0


@pytest.mark.parametrize("variant", ["youtube-html-en", "youtube-html-ro"])
def test_json_html_equivalence(variant: str) -> None:
    def normalise(name: str) -> list[dict[str, object]]:
        fixture = FIXTURES[name]
        return [
            row.model_dump(exclude={"source_file"})
            for row in CONNECTOR.parse(export_path(fixture), load_meta(fixture))
        ]

    assert normalise(variant) == normalise("youtube-json-en")


def test_html_chunking_and_unicode() -> None:
    fixture = FIXTURES["youtube-html-ro"]
    path = next(export_path(fixture).rglob("*.html"))
    data = b" " * 4095 + path.read_bytes() * 50

    class BoundedReader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size == 4096
            return super().read(size)

    stream: IO[bytes] = BoundedReader(data)
    parsed = list(records(stream, "html"))
    assert len(parsed) == 50
    assert all(record["title"] == "Ați vizionat Synthetic stars" for record in parsed)


def test_removed_ads_and_malformed_counts(caplog: pytest.LogCaptureFixture) -> None:
    fixture = FIXTURES["removed-and-ads"]
    with caplog.at_level(logging.INFO):
        parsed = list(CONNECTOR.parse(export_path(fixture), load_meta(fixture)))
    assert len(parsed) == 4
    assert [row.title for row in parsed[:2]] == [None, None]
    assert all(row.meta is not None and row.meta["from_ads"] for row in parsed[2:])
    assert caplog.messages == ["activity skipped: count=2"]


def test_search_visits_count(caplog: pytest.LogCaptureFixture) -> None:
    fixture = FIXTURES["search-html-en"]
    with caplog.at_level(logging.INFO):
        parsed = list(CONNECTOR.parse(export_path(fixture), load_meta(fixture)))
    assert len(parsed) == 1
    assert parsed[0].title == "Synthetic stars"
    assert caplog.messages == ["activity skipped: count=1"]


def test_chrome_fields_and_fractional_timestamp() -> None:
    fixture = FIXTURES["chrome-json"]
    [row] = CONNECTOR.parse(export_path(fixture), load_meta(fixture))
    assert row.ts == datetime(2024, 1, 1, 10, 0, 0, 123456, tzinfo=UTC)
    assert row.meta == {"page_transition": "LINK"}


def test_duplicate_occurrences_and_overlap(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text(json.dumps([sample(), sample()]))
    other = tmp_path / "renamed.json"
    other.write_text(json.dumps([sample()]))
    rows = list(CONNECTOR.parse(path, context(path)))
    assert rows[0].source_row_id != rows[1].source_row_id
    [overlap] = CONNECTOR.parse(other, context(other))
    assert overlap.source_row_id == rows[0].source_row_id
    with Store.open(tmp_path / "life.duckdb") as store:
        assert run_import(store, CONNECTOR, path, context(path)).inserted == 2
        assert run_import(store, CONNECTOR, path, context(path)).inserted == 0
        assert run_import(store, CONNECTOR, other, context(other)).inserted == 0


def test_bad_chrome_records_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "BrowserHistory.json"
    path.write_text(
        json.dumps(
            {"Browser History": [{}, {"time_usec": "wrong"}, {"time_usec": 1704067200000000}]}
        )
    )
    with caplog.at_level(logging.INFO):
        rows = list(CONNECTOR.parse(path, context(path)))
    assert len(rows) == 1
    assert caplog.messages == ["activity skipped: count=2"]


def test_streaming_50mb(tmp_path: Path) -> None:
    path = tmp_path / "History.json"
    GENERATOR.write(path, 50 * 2**20, 17)
    assert path.stat().st_size >= 50 * 2**20
    assert_streaming(CONNECTOR, path, max_rss_mb=200)


def test_search_json_html_equivalence(tmp_path: Path) -> None:
    fixture = FIXTURES["search-html-en"]
    [html_row] = CONNECTOR.parse(export_path(fixture), load_meta(fixture))
    path = tmp_path / "activity.json"
    path.write_text(
        json.dumps(
            [
                dict(
                    sample("Searched for ignored"),
                    header="Search",
                    products=["Search"],
                    titleUrl="https://example.com/search?q=Synthetic+stars",
                )
            ]
        )
    )
    [json_row] = CONNECTOR.parse(path, context(path))
    assert json_row.model_dump(exclude={"source_file"}) == html_row.model_dump(
        exclude={"source_file"}
    )


def test_html_typography_header_and_ads(tmp_path: Path) -> None:
    markup = (
        '<div class="outer-cell"><p class="mdl-typography-title">YouTube<br></p>'
        '<div class="content-cell">Has visto <a href="https://example.com/video">'
        "Synthetic stars</a><br>1 ene. 2024, 12:00:00 EET<br>De Google Ads</div></div>"
    )
    [record] = records(io.BytesIO(markup.encode()), "html")
    event = activity(record, context(tmp_path), True)
    assert event is not None
    assert event.title == "Synthetic stars"
    assert event.meta == {"channel_name": None, "channel_url": None, "from_ads": True}


def test_null_title_and_empty_search_parameter(tmp_path: Path) -> None:
    record = dict(sample(), title=None)
    event = activity(record, context(tmp_path), False)
    assert event is not None
    assert event.title is None
    record = dict(
        sample("Searched for ignored"),
        header="Search",
        products=["Search"],
        titleUrl="https://example.com/search?q=",
    )
    event = activity(record, context(tmp_path), False)
    assert event is not None
    assert event.title is None


def test_detection_sampling_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    import sherd_connectors.google_takeout as takeout

    sizes: list[int] = []

    class SampleReader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size == 65536
            sizes.append(size)
            return super().read(size)

    @contextmanager
    def candidates(path: Path) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
        def members() -> Iterator[tuple[str, IO[bytes]]]:
            for index in range(20):
                yield str(index), SampleReader(b"x" * 100000)

        yield members()

    monkeypatch.setattr(takeout, "files", candidates)
    assert CONNECTOR.detect(tmp_path).confidence == 0
    assert sizes == [65536] * 5
