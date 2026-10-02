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
from sherd_connectors.google_takeout.dates import parse_date
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
    assert caplog.messages == ["activity skipped: visited=0 unsupported=0 malformed=2"]


def test_search_visits_count(caplog: pytest.LogCaptureFixture) -> None:
    fixture = FIXTURES["search-html-en"]
    with caplog.at_level(logging.INFO):
        parsed = list(CONNECTOR.parse(export_path(fixture), load_meta(fixture)))
    assert len(parsed) == 1
    assert parsed[0].title == "Synthetic stars"
    assert caplog.messages == ["activity skipped: visited=1 unsupported=0 malformed=0"]


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
    assert caplog.messages == ["activity skipped: visited=0 unsupported=0 malformed=2"]


def test_streaming_50mb(tmp_path: Path) -> None:
    path = tmp_path / "History.json"
    GENERATOR.write(path, 50 * 2**20, 17)
    assert path.stat().st_size >= 50 * 2**20
    assert path.read_bytes()[:1024].count(b"time_usec") >= 8
    assert_streaming(CONNECTOR, path, max_rss_mb=100)


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
    def candidates(
        path: Path, *, detecting: bool = False
    ) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
        def members() -> Iterator[tuple[str, IO[bytes]]]:
            for index in range(40):
                yield str(index), SampleReader(b"x" * 100000)

        yield members()

    monkeypatch.setattr(takeout, "files", candidates)
    assert CONNECTOR.detect(tmp_path).confidence == 0
    assert sizes == [65536] * 32


@pytest.mark.parametrize(
    "prefix", ["Searched for ", "Ați căutat ", "Gesucht nach: ", "Has buscado ", "Buscaste "]
)
@pytest.mark.parametrize("html", [False, True])
def test_youtube_search(prefix: str, html: bool, tmp_path: Path) -> None:
    record = dict(
        sample(prefix + "synthetic angesehen"), titleUrl="https://example.com/results?q=ignored"
    )
    if html:
        markup = (
            '<div class="outer-cell"><div class="header-cell">YouTube</div>'
            '<div class="content-cell">'
            + prefix
            + '<a href="https://example.com/results?q=ignored">'
            "synthetic angesehen</a><br>Jan 1, 2024, 10:00:00 AM UTC</div>"
            '<div class="content-cell"></div>'
            '<div class="content-cell">Products:<br>YouTube</div></div>'
        )
        [record] = records(io.BytesIO(markup.encode()), "html")
    event = activity(record, context(tmp_path), html)
    assert event is not None
    assert event.kind == "youtube.search"
    assert event.title == "synthetic angesehen"
    assert event.url == "https://example.com/results?q=ignored"
    assert event.meta is None


@pytest.mark.parametrize("action", ["Liked ", "Viewed "])
@pytest.mark.parametrize(
    "url", ["https://example.com/video", "https://example.com/watch?v=synthetic"]
)
def test_youtube_other_actions_skipped(action: str, url: str, tmp_path: Path) -> None:
    assert (
        activity(
            dict(sample(action + "Synthetic stars"), titleUrl=url),
            context(tmp_path),
            False,
        )
        is None
    )


@pytest.mark.parametrize("space", ["\xa0", "\u202f", "\u2003", "\u2009"])
@pytest.mark.parametrize("prefix", ["Watched", "Ați vizionat", "Angesehen:", "Has visto"])
def test_unicode_spaces(space: str, prefix: str, tmp_path: Path) -> None:
    event = activity(sample(prefix + space + "Synthetic stars"), context(tmp_path), False)
    assert event is not None
    assert event.title == "Synthetic stars"
    assert parse_date(
        f"1{space}abr.{space}2024, 12:00:00{space}p.{space}m.{space}CET", ZoneInfo("Asia/Tokyo")
    ) == datetime(2024, 4, 1, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "months",
    [
        "jan feb mar apr may jun jul aug sep oct nov dec",
        "ian feb mar apr mai iun iul aug sept oct nov dec",
        "jan feb mär apr mai jun jul aug sep okt nov dez",
        "ene feb mar abr may jun jul ago sept oct nov dic",
        "janv févr mars avr mai juin juil août sept oct nov déc",
    ],
)
def test_all_localised_months(months: str) -> None:
    for month, name in enumerate(months.split(), 1):
        assert parse_date(f"1 {name}. 2024, 12:00:00 UTC", ZoneInfo("UTC")) == datetime(
            2024, month, 1, 12, tzinfo=UTC
        )


@pytest.mark.parametrize(
    ("clock", "hour"),
    [("12:00:00 p. m.", 12), ("12:00:00 a. m.", 0), ("1:00:00 p. m.", 13), ("1:00:00 a. m.", 1)],
)
def test_spanish_meridiem(clock: str, hour: int) -> None:
    assert parse_date(f"1 ago. 2024, {clock} UTC", ZoneInfo("Asia/Tokyo")) == datetime(
        2024, 8, 1, hour, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("zone", "hour"), [("GMT+2", 10), ("UTC-03:30", 15), ("CET", 11), ("EET", 10), ("PST", 20)]
)
def test_explicit_timezone_offsets(zone: str, hour: int) -> None:
    assert parse_date(f"Jan 1, 2024, 12:00:00 PM {zone}", ZoneInfo("Asia/Tokyo")).hour == hour


def test_occurrences_reset_on_timestamp_change(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    later = dict(sample(), time="2024-01-01T11:00:00Z")
    path.write_text(json.dumps([sample(), sample(), later, sample()]))
    rows = list(CONNECTOR.parse(path, context(path)))
    assert rows[0].source_row_id != rows[1].source_row_id
    assert rows[0].source_row_id == rows[3].source_row_id


def test_german_search_suffix_preserved(tmp_path: Path) -> None:
    event = activity(
        dict(sample("Gesucht nach: synthetic angesehen"), header="Search", products=["Search"]),
        context(tmp_path),
        False,
    )
    assert event is not None
    assert event.title == "synthetic angesehen"


def test_naive_json_timestamp_and_empty_chrome_title(tmp_path: Path) -> None:
    event = activity(dict(sample(), time="2024-01-01T10:00:00"), context(tmp_path), False)
    assert event is not None
    assert event.ts == datetime(2024, 1, 1, 10, tzinfo=UTC)
    path = tmp_path / "History.json"
    path.write_text(json.dumps({"Browser History": [{"time_usec": 1704067200000000, "title": ""}]}))
    [row] = CONNECTOR.parse(path, context(path))
    assert row.title is None


@pytest.mark.parametrize("zip_export", [False, True])
def test_detect_activity_after_many_unrelated_files(tmp_path: Path, zip_export: bool) -> None:
    for index in range(40):
        (tmp_path / f"Access-{index:02}.json").write_text("[]")
    (tmp_path / "My Activity").mkdir()
    (tmp_path / "My Activity/history.json").write_text(json.dumps([sample()]))
    path = tmp_path
    if zip_export:
        import zipfile

        path = tmp_path / "takeout.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for file in tmp_path.rglob("*.json"):
                archive.write(file, file.relative_to(tmp_path))
    assert CONNECTOR.detect(path).confidence == 0.95


def test_skip_reasons_separate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            [
                sample("Visited synthetic"),
                dict(sample(), header="Other", products=["Other"]),
                dict(sample(), time="invalid"),
            ]
        )
    )
    with caplog.at_level(logging.INFO):
        assert list(CONNECTOR.parse(path, context(path))) == []
    assert caplog.messages == ["activity skipped: visited=1 unsupported=1 malformed=1"]


@pytest.mark.parametrize(
    ("zone", "offset"),
    [
        ("UTC", 0),
        ("GMT", 0),
        ("WET", 0),
        ("CET", 1),
        ("MEZ", 1),
        ("BST", 1),
        ("WEST", 1),
        ("EET", 2),
        ("CEST", 2),
        ("MESZ", 2),
        ("EEST", 3),
        ("EST", -5),
        ("CDT", -5),
        ("EDT", -4),
        ("CST", -6),
        ("MDT", -6),
        ("MST", -7),
        ("PDT", -7),
        ("PST", -8),
    ],
)
def test_timezone_table(zone: str, offset: int) -> None:
    assert parse_date(f"Jan 1, 2024, 12:00:00 PM {zone}", ZoneInfo("Asia/Tokyo")) == datetime(
        2024, 1, 1, 12 - offset, tzinfo=UTC
    )


def test_multicell_body_and_caption_details() -> None:
    fixture = FIXTURES["youtube-html-details"]
    [row] = CONNECTOR.parse(export_path(fixture), load_meta(fixture))
    assert row.title == "Synthetic stars"
    assert row.ts == datetime(2024, 1, 1, 10, tzinfo=UTC)
    assert row.url == "https://example.com/watch?v=synthetic"
    assert row.meta == {
        "channel_name": "Fictional Studio",
        "channel_url": "https://example.com/channel/demo",
        "from_ads": True,
    }


@pytest.mark.parametrize("month", ["sep", "sept", "set", "septiembre"])
def test_spanish_september_aliases(month: str) -> None:
    assert parse_date(f"1 {month}. 2024, 12:00:00 UTC", ZoneInfo("UTC")) == datetime(
        2024, 9, 1, 12, tzinfo=UTC
    )
