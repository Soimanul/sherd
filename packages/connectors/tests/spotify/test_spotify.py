import json
import logging
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.base import ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_connectors.registry import discover
from sherd_connectors.spotify import CONNECTOR
from sherd_connectors.spotify.synth_spotify import GENERATOR
from sherd_connectors.testing import assert_streaming, export_path, load_meta
from sherd_core import MediaPlay, Store, content_hash

FIXTURES = CONNECTOR.fixtures()


def context(path: Path) -> ImportContext:
    return ImportContext(export_root(path), ZoneInfo("America/Chicago"), frozenset())


def variant(name: str) -> Path:
    return next(p for p in FIXTURES if p.name == name)


def rows(name: str) -> list[MediaPlay]:
    fixture = variant(name)
    return list(CONNECTOR.parse(export_path(fixture), load_meta(fixture)))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_detect_directory_and_single_files(fixture: Path) -> None:
    path = export_path(fixture)
    assert CONNECTOR.detect(path).confidence == 0.99
    for file in path.rglob("*.json"):
        assert CONNECTOR.detect(file).confidence == 0.99


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_import_twice_is_idempotent(fixture: Path, tmp_path: Path) -> None:
    path = export_path(fixture)
    with Store.open(tmp_path / "life.duckdb") as store:
        first = run_import(store, CONNECTOR, path, load_meta(fixture))
        second = run_import(store, CONNECTOR, path, load_meta(fixture))
        assert first.inserted == len(list(CONNECTOR.parse(path, load_meta(fixture)))) > 0
        assert second.inserted == 0
        assert store.table_counts()["media_plays"] == first.inserted


def test_overlap_preserves_occurrences_and_collapses_files() -> None:
    parsed = rows("extended-audio")
    assert len(parsed) == 5
    repeats = [r for r in parsed if r.track == "Fictional Orbit"]
    assert len(repeats) == 2
    assert repeats[0].source_row_id != repeats[1].source_row_id
    assert all(r.source_file.endswith("2024_0.json") for r in parsed)
    for index, row in enumerate(repeats):
        assert row.source_row_id == content_hash(
            "2024-06-01T12:34:00+00:00", "track", row.artist, row.track, 180000, index
        )


def test_cross_format_overlap_uses_extended_first() -> None:
    parsed = rows("overlap-cross-format")
    assert len(parsed) == 3
    assert all(r.uri is not None and "Streaming_History_" in r.source_file for r in parsed)
    assert {r.source_row_id for r in parsed} == {
        r.source_row_id for r in rows("account-music") if r.track is not None
    } | {r.source_row_id for r in rows("account-podcast") if r.track is not None}


@pytest.mark.parametrize("extended_first", [True, False])
def test_separate_cross_format_imports_first_write_wins(
    extended_first: bool, tmp_path: Path
) -> None:
    fixture = variant("overlap-cross-format")
    files = list(export_path(fixture).rglob("*.json"))
    files.sort(key=lambda p: (p.name.startswith("Streaming_History_") != extended_first, p.name))
    with Store.open(tmp_path / "life.duckdb") as store:
        inserted = [run_import(store, CONNECTOR, p, context(p)).inserted for p in files]
        assert sum(inserted) == 3
        assert store.table_counts()["media_plays"] == 3
        uri = store.query("SELECT uri FROM media_plays").column("uri").to_pylist()
        assert all((u is not None) == extended_first for u in uri)


def test_dropped_fields_nulls_and_count_only_logging(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sherd.connectors.spotify"):
        parsed = rows("extended-old-fields")
    assert len(parsed) == 3
    assert [record.getMessage() for record in caplog.records] == [
        "parse skipped records: connector=spotify count=6"
    ]
    serialized = json.dumps([r.model_dump(mode="json") for r in parsed])
    for forbidden in (
        "ip_addr",
        "192.0.2.1",
        "username",
        "fictional@example.com",
        "user_agent_decrypted",
        "Synthetic Agent",
        "offline_timestamp",
    ):
        assert forbidden not in serialized
        assert forbidden not in caplog.text
    assert all(r.skipped is None for r in parsed)
    assert parsed[1].track is None
    assert parsed[1].uri is None
    assert parsed[2].ms_played == 0
    assert parsed[2].meta == dict.fromkeys(
        ("reason_start", "reason_end", "offline", "incognito_mode", "conn_country")
    )


def test_media_kind_precedence_and_mapping() -> None:
    parsed = rows("extended-video")
    assert [r.media_kind for r in parsed] == ["video", "episode", "audiobook"]
    assert parsed[1].artist == "Fictional Observatory"
    assert parsed[1].track == "Imaginary Episode One"
    assert parsed[1].uri == "spotify:episode:syntheticOne"
    assert parsed[2].artist == "Fictional Moon Atlas"
    assert parsed[2].track == "Imaginary Chapter One"
    assert parsed[2].uri == "spotify:chapter:syntheticOne"


def test_account_variants_and_utc_independent_of_context() -> None:
    for name in ("account-music", "account-podcast", "account-legacy-name"):
        for row in rows(name):
            assert row.ts.tzinfo == UTC
            assert (row.album, row.uri, row.shuffle, row.skipped, row.platform, row.meta) == (
                None,
                None,
                None,
                None,
                None,
                None,
            )
    assert rows("account-music")[0].ts == datetime(2024, 6, 1, 12, 34, tzinfo=UTC)
    assert [r.media_kind for r in rows("account-legacy-name")] == ["track", "episode"]


def test_zip_streaming_without_extraction(tmp_path: Path) -> None:
    source = export_path(variant("overlap-cross-format"))
    target = tmp_path / "export.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in source.rglob("*.json"):
            archive.write(file, file.relative_to(source))
        archive.writestr("unrelated.json", '[{"private":"not history"}]')
    assert CONNECTOR.detect(target).confidence == 0.99
    parsed = list(CONNECTOR.parse(target, context(target)))
    assert len(parsed) == 3
    assert [r.source_row_id for r in parsed] == [
        r.source_row_id for r in rows("overlap-cross-format")
    ]
    assert all(r.source_file.startswith("export.zip/") for r in parsed)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    ("name", "content", "confidence"),
    [
        ("StreamingHistory0.json", "[]", 0.6),
        ("StreamingHistory0.json", "{}", 0.0),
        ("arbitrary.json", '[{"ts":"2024-01-01","ms_played":10}]', 0.0),
        ("_chat.txt", "01/01/24, 12:00 - Fictional: hello", 0.0),
        ("history.json", '[{"url":"https://example.com"}]', 0.0),
        ("bank.csv", "Date,Amount,Currency\n2024-01-01,1,EUR", 0.0),
        (".zsh_history", ": 1704067200:0;echo synthetic", 0.0),
        ("export.zip", "invalid zip", 0.0),
    ],
)
def test_detect_confidences_and_negative_exports(
    tmp_path: Path, name: str, content: str, confidence: float
) -> None:
    path = tmp_path / name
    path.write_text(content)
    assert CONNECTOR.detect(path).confidence == confidence
    assert CONNECTOR.detect(tmp_path / "missing").confidence == 0.0


def test_negative_on_other_connector_fixtures() -> None:
    for connector in discover().values():
        if connector.id != CONNECTOR.id:
            for fixture in connector.fixtures():
                assert CONNECTOR.detect(export_path(fixture)).confidence == 0.0


def test_detect_reads_only_three_bounded_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(5):
        (tmp_path / f"StreamingHistory{index}.json").write_text("[]")
    reads: list[int] = []

    class Prefix:
        def read(self, size: int) -> bytes:
            reads.append(size)
            return b"[" + b" " * size

    from contextlib import contextmanager

    @contextmanager
    def fake_files(path: Path, ctx: ImportContext) -> Iterator[Iterator[tuple[str, Prefix]]]:
        yield iter((f"StreamingHistory{i}.json", Prefix()) for i in range(5))

    monkeypatch.setattr("sherd_connectors.spotify._files", fake_files)
    assert CONNECTOR.detect(tmp_path).confidence == 0.6
    assert reads == [64 * 1024] * 3


def test_generator_is_deterministic(tmp_path: Path) -> None:
    paths = [tmp_path / f"Streaming_History_Audio_2024_{i}.json" for i in range(2)]
    for path in paths:
        GENERATOR.write(path, 4096, seed=5)
        assert path.stat().st_size >= 4096
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert list(CONNECTOR.parse(paths[0], context(paths[0])))
    with pytest.raises(ValueError, match="nonnegative"):
        GENERATOR.write(paths[0], -1, seed=5)


def test_streaming_50mb_under_200mb(tmp_path: Path) -> None:
    path = tmp_path / "Streaming_History_Audio_2024_0.json"
    GENERATOR.write(path, 50 * 1024**2, seed=5)
    assert path.stat().st_size >= 50 * 1024**2
    assert_streaming(CONNECTOR, path, max_rss_mb=200)


def test_invalid_json_fails_and_marks_import_failed(tmp_path: Path) -> None:
    from ijson.common import JSONError  # type: ignore[import-untyped]  # No upstream type metadata.

    path = tmp_path / "StreamingHistory0.json"
    path.write_text('[{"endTime":')
    with Store.open(tmp_path / "life.duckdb") as store:
        with pytest.raises(JSONError):
            run_import(store, CONNECTOR, path, context(path))
        assert store.query("SELECT status FROM imports").column("status").to_pylist() == ["failed"]


def test_invalid_durations_skipped_and_offset_timestamps_normalized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "Streaming_History_Audio_2024_0.json"
    record = {"ts": "2024-06-01T15:34:56+03:00", "ms_played": 0}
    path.write_text(
        json.dumps([record] + [record | {"ms_played": n} for n in [-1, True, "10", 1.5]])
    )
    parsed = list(CONNECTOR.parse(path, context(path)))
    assert len(parsed) == 1
    assert parsed[0].ts == datetime(2024, 6, 1, 12, 34, 56, tzinfo=UTC)
    assert caplog.messages == ["parse skipped records: connector=spotify count=4"]
