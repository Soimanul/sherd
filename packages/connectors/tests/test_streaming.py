import random
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sherd_connectors.base import DetectResult, ImportContext
from sherd_connectors.synth.raw import RawGenerator
from sherd_connectors.testing import assert_streaming, measure_streaming
from sherd_core import Event, Row

MB = 2**20


class StreamingLines:
    """Reads `ts|title` lines one at a time."""

    id = "streaming_lines"
    version = "1"
    display_name = "Streaming lines"

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(0.0, "test only")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines):
                stamp, title = line.rstrip("\n").split("|", 1)
                yield Event(
                    source_file=path.name,
                    source_row_id=str(number),
                    ts=datetime.fromisoformat(stamp),
                    kind="shell.command",
                    title=title,
                )

    def fixtures(self) -> list[Path]:
        return []


class LoadingLines(StreamingLines):
    """The anti-pattern: reads the whole file and builds every row before yielding."""

    id = "loading_lines"

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        text = path.read_text(encoding="utf-8")
        rows = list(super().parse(path, ctx))
        assert text
        return iter(rows)


class LinesRaw:
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        rng = random.Random(seed)
        start = datetime(2024, 1, 1, tzinfo=UTC)
        words = ["git", "status", "ls", "-la", "uv", "run", "pytest", "make", "build", "cd"]
        written = 0
        with path.open("w", encoding="utf-8") as out:
            number = 0
            while written < approx_bytes:
                chunk = []
                for _ in range(1000):
                    stamp = (start + timedelta(seconds=number)).isoformat()
                    title = " ".join(rng.choices(words, k=60))
                    chunk.append(f"{stamp}|{title}\n")
                    number += 1
                block = "".join(chunk)
                out.write(block)
                written += len(block)


@pytest.fixture(scope="module")
def big_export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("streaming") / "history.lines"
    generator: RawGenerator = LinesRaw()
    generator.write(path, 52 * MB, seed=7)
    return path


def test_streaming_parse_stays_under_200_mb(big_export: Path) -> None:
    assert big_export.stat().st_size >= 50 * MB
    assert_streaming(StreamingLines(), big_export, max_rss_mb=200)


def test_prior_memory_heavy_child_does_not_affect_measurement(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "data = bytearray(420 * 2**20)\n"
            "for offset in range(0, len(data), 4096): data[offset] = 1\n",
        ],
        check=True,
    )
    export = tmp_path / "small.lines"
    export.write_text("2024-01-01T00:00:00+00:00|git status\n", encoding="utf-8")
    assert_streaming(StreamingLines(), export, max_rss_mb=200)


def test_loading_parse_is_caught(big_export: Path) -> None:
    streaming = measure_streaming(StreamingLines(), big_export)
    loading = measure_streaming(LoadingLines(), big_export)
    assert streaming["rows"] == loading["rows"] > 100_000
    assert loading["peak_rss_mb"] > streaming["peak_rss_mb"] + 50
    with pytest.raises(AssertionError, match="parse must stream"):
        assert_streaming(LoadingLines(), big_export, max_rss_mb=streaming["peak_rss_mb"] + 25)


def test_child_failure_is_reported(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="parse failed in the child process"):
        measure_streaming(StreamingLines(), tmp_path / "missing.lines")
