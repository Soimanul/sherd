import json
import os
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sherd_connectors import testing
from sherd_connectors.base import DetectResult, ImportContext
from sherd_core import Event, Row, content_hash


class LinesConnector:
    """A test-only connector: `*.lines` files of `local-iso-ts|kind|title`."""

    id = "lines"
    version = "1"
    display_name = "Lines"

    def __init__(self, fixtures_root: Path | None = None) -> None:
        self.fixtures_root = fixtures_root

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(0.9 if path.suffix == ".lines" else 0.0, "suffix")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        for file in sorted(path.rglob("*.lines")) if path.is_dir() else [path]:
            source_file = file.relative_to(ctx.export_root).as_posix()
            occurrences: Counter[str] = Counter()
            with file.open(encoding="utf-8") as lines:
                for line in lines:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    stamp, kind, title = line.split("|", 2)
                    occurrences[line] += 1
                    yield Event(
                        source_file=source_file,
                        source_row_id=content_hash(line, occurrences[line]),
                        ts=datetime.fromisoformat(stamp).replace(tzinfo=ctx.tz),
                        kind=kind,
                        title=title,
                    )

    def fixtures(self) -> list[Path]:
        if self.fixtures_root is None:
            return []
        return [path for path in self.fixtures_root.iterdir() if path.is_dir()]


LINES = [
    "2024-03-01T09:00:00|shell.command|git status",
    "2024-03-01T09:00:05|shell.command|ls",
    "2024-03-01T09:00:05|shell.command|ls",
    "2024-03-02T22:15:00|google.search|weather tomorrow",
]


def make_variant(root: Path, name: str = "basic", **meta: object) -> Path:
    variant = root / name
    (variant / "export" / "sub").mkdir(parents=True)
    (variant / "export" / "sub" / "history.lines").write_text("\n".join(LINES) + "\n")
    meta = {"tz": "Europe/Bucharest", "self_identities": ["Alex Demo"], **meta}
    (variant / "meta.json").write_text(json.dumps(meta))
    return variant


@pytest.fixture
def variant(tmp_path: Path) -> Path:
    return make_variant(tmp_path)


@pytest.fixture
def connector(tmp_path: Path) -> LinesConnector:
    return LinesConnector(tmp_path)


def golden(variant: Path) -> list[dict[str, object]]:
    lines = (variant / "expected.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_variants_and_meta(connector: LinesConnector, variant: Path) -> None:
    assert testing.variants(connector) == [variant]
    ctx = testing.load_meta(variant)
    assert ctx.tz.key == "Europe/Bucharest"
    assert ctx.self_identities == frozenset({"Alex Demo"})
    assert ctx.export_root == variant / "export"
    assert testing.export_path(variant) == variant / "export"


def test_meta_can_point_at_a_single_file(tmp_path: Path, connector: LinesConnector) -> None:
    variant = make_variant(tmp_path, "single", export="export/sub/history.lines")
    ctx = testing.load_meta(variant)
    assert testing.export_path(variant) == variant / "export/sub/history.lines"
    assert ctx.export_root == variant / "export/sub"
    rows = testing.canonical_rows(connector, variant)
    assert {row["source_file"] for row in rows} == {"history.lines"}


def test_canonical_rows(connector: LinesConnector, variant: Path) -> None:
    rows = testing.canonical_rows(connector, variant)
    assert len(rows) == 4  # the repeated `ls` keeps both occurrences
    assert all(row["table"] == "events" for row in rows)
    assert [row["source_row_id"] for row in rows] == sorted(row["source_row_id"] for row in rows)
    assert not testing.STORE_FIELDS & rows[0].keys()
    first = next(row for row in rows if row["title"] == "git status")
    assert first["ts"] == "2024-03-01T07:00:00Z"  # 09:00 in Bucharest, stored as UTC
    assert first["source_file"] == "sub/history.lines"


def test_canonical_rows_collapse_identical_duplicates(tmp_path: Path) -> None:
    class Twice(LinesConnector):
        def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
            for row in super().parse(path, ctx):
                yield row
                yield row

    variant = make_variant(tmp_path)
    rows = testing.canonical_rows(Twice(tmp_path), variant)
    assert len(rows) == 4
    assert rows == testing.canonical_rows(LinesConnector(tmp_path), variant)


def test_update_writes_golden_then_passes(
    connector: LinesConnector, variant: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(testing.UPDATE_ENV, "1")
    testing.assert_golden(connector, variant)
    assert golden(variant) == testing.canonical_rows(connector, variant)
    monkeypatch.delenv(testing.UPDATE_ENV)
    testing.assert_golden(connector, variant)


def test_missing_golden_explains_how_to_create_it(connector: LinesConnector, variant: Path) -> None:
    with pytest.raises(AssertionError, match=f"{testing.UPDATE_ENV}=1"):
        testing.assert_golden(connector, variant)


def test_failure_shows_a_short_readable_diff(connector: LinesConnector, variant: Path) -> None:
    testing.write_golden(connector, variant)
    rows = golden(variant)
    changed = next(row for row in rows if row["title"] == "weather tomorrow")
    changed["title"] = "weather today"
    dropped = rows.pop(0)
    rows.append({**dropped, "source_row_id": "ghost"})
    rows.sort(key=lambda row: (str(row["table"]), str(row["source_row_id"])))
    (variant / "expected.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    with pytest.raises(AssertionError) as error:
        testing.assert_golden(connector, variant)
    message = str(error.value)
    assert f"events/{changed['source_row_id']} title: expected 'weather today'" in message
    assert "got 'weather tomorrow'" in message
    assert f"unexpected row events/{dropped['source_row_id']}" in message
    assert "missing row events/ghost" in message
    assert len(message.splitlines()) < 10


def test_diff_is_capped(connector: LinesConnector, tmp_path: Path) -> None:
    variant = make_variant(tmp_path)
    many = [f"2024-03-01T10:{minute:02d}:00|shell.command|cmd {minute}" for minute in range(30)]
    (variant / "export" / "sub" / "more.lines").write_text("\n".join(many) + "\n")
    (variant / "expected.jsonl").write_text("")
    with pytest.raises(AssertionError) as error:
        testing.assert_golden(connector, variant)
    assert "and 29 more unexpected rows" in str(error.value)
    assert len(str(error.value).splitlines()) < 10


def test_update_rewrites_a_stale_golden(
    connector: LinesConnector, variant: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (variant / "expected.jsonl").write_text('{"table": "events", "source_row_id": "old"}\n')
    with pytest.raises(AssertionError):
        testing.assert_golden(connector, variant)
    monkeypatch.setenv(testing.UPDATE_ENV, "1")
    testing.assert_golden(connector, variant)
    monkeypatch.delenv(testing.UPDATE_ENV)
    testing.assert_golden(connector, variant)
    assert "old" not in (variant / "expected.jsonl").read_text()


@pytest.mark.parametrize("update", [False, True])
def test_conflicting_duplicate_raises_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: bool
) -> None:
    class Conflict(LinesConnector):
        def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
            for row in super().parse(path, ctx):
                yield row
                yield row.model_copy(update={"title": "conflicting private content"})

    variant = make_variant(tmp_path)
    connector = LinesConnector(tmp_path)
    testing.write_golden(connector, variant)
    baseline = (variant / "expected.jsonl").read_bytes()
    first = next(connector.parse(testing.export_path(variant), testing.load_meta(variant)))
    if update:
        monkeypatch.setenv(testing.UPDATE_ENV, "1")
    with pytest.raises(testing.IdCollisionError) as error:
        testing.assert_golden(Conflict(tmp_path), variant)
    assert str(error.value) == (
        f"ID collision in events for source_row_id {first.source_row_id!r}; differing fields: title"
    )
    assert "conflicting private content" not in str(error.value)
    assert (variant / "expected.jsonl").read_bytes() == baseline


def test_update_parse_failure_preserves_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(LinesConnector):
        def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
            yield next(super().parse(path, ctx))
            raise ValueError("invalid record")

    variant = make_variant(tmp_path)
    testing.write_golden(LinesConnector(tmp_path), variant)
    baseline = (variant / "expected.jsonl").read_bytes()
    monkeypatch.setenv(testing.UPDATE_ENV, "1")
    with pytest.raises(ValueError, match="invalid record"):
        testing.assert_golden(Broken(tmp_path), variant)
    assert (variant / "expected.jsonl").read_bytes() == baseline
    assert not list(variant.glob(".expected-*.jsonl.tmp"))


def test_update_replace_failure_preserves_golden(
    connector: LinesConnector, variant: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    testing.write_golden(connector, variant)
    baseline = (variant / "expected.jsonl").read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        testing.write_golden(connector, variant)
    assert (variant / "expected.jsonl").read_bytes() == baseline
    assert not list(variant.glob(".expected-*.jsonl.tmp"))
