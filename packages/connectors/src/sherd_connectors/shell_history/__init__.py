"""Streaming zsh, timestamped bash and atuin shell-history connector."""

import logging
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from zipfile import ZipFile, is_zipfile

from sherd_core import Event, content_hash

from sherd_connectors.base import DetectResult, ImportContext
from sherd_connectors.shell_history.redaction import redact

_LOG = logging.getLogger(__name__)
_ZSH = re.compile(r"^: (\d+):(\d+);(.*)$")
_BASH = re.compile(r"^#(\d+)$")
_NAMES = {".zsh_history", ".bash_history", "history.db"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def unmetafy(data: bytes) -> str:
    """Unescape zsh's Meta byte before decoding UTF-8."""
    output = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        if value == 0x83 and index + 1 < len(data):
            index += 1
            value = data[index] ^ 0x20
        output.append(value)
        index += 1
    return output.decode("utf-8", errors="replace")


@contextmanager
def _sources(path: Path) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    if path.is_file() and is_zipfile(path):
        with ZipFile(path) as archive:

            def members() -> Iterator[tuple[str, IO[bytes]]]:
                for name in sorted(archive.namelist()):
                    if Path(name).name in _NAMES:
                        with archive.open(name) as stream:
                            yield name, stream

            yield members()
    else:

        def files() -> Iterator[tuple[str, IO[bytes]]]:
            candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
            for file in candidates:
                if file.is_file() and file.name in _NAMES:
                    name = file.relative_to(path).as_posix() if path.is_dir() else file.name
                    with file.open("rb") as stream:
                        yield name, stream

        yield files()


def _text_records(stream: IO[bytes], shell: str) -> Iterator[tuple[int, int | None, str]]:
    epoch: int | None = None
    duration: int | None = None
    parts: list[str] = []
    continued = False
    skipped = 0
    for raw in stream:
        line = (unmetafy(raw) if shell == "zsh" else raw.decode("utf-8", "replace")).rstrip("\r\n")
        if shell == "zsh":
            if continued:
                parts.append(line)
            else:
                match = _ZSH.fullmatch(line)
                if match is None:
                    skipped += 1
                    continue
                epoch, duration = int(match[1]), int(match[2]) * 1000
                parts = [match[3]]
            continued = line.endswith("\\")
            if not continued and epoch is not None:
                yield epoch, duration, "\n".join(parts)
                parts = []
        else:
            match = _BASH.fullmatch(line)
            if match is not None:
                if epoch is not None and parts:
                    yield epoch, None, "\n".join(parts)
                epoch, parts = int(match[1]), []
            elif epoch is not None:
                parts.append(line)
            else:
                skipped += 1
    if parts and epoch is not None:
        yield epoch, duration, "\n".join(parts)
    _LOG.info("shell history skipped lines=%d", skipped)


class ShellHistoryConnector:
    id = "shell_history"
    version = "1"
    display_name = "Shell history"

    def detect(self, path: Path) -> DetectResult:
        try:
            with _sources(path) as sources:
                for index, (name, stream) in enumerate(sources):
                    if index == 3:
                        break
                    sample = stream.read(65536)
                    basename = Path(name).name
                    if basename == "history.db" and sample.startswith(b"SQLite format 3\x00"):
                        return DetectResult(0.95, "atuin SQLite history")
                    if basename == ".zsh_history" and any(
                        _ZSH.fullmatch(line) for line in unmetafy(sample).splitlines()
                    ):
                        return DetectResult(1.0, "zsh extended history")
                    if basename == ".bash_history" and any(
                        _BASH.fullmatch(line)
                        for line in sample.decode("utf-8", "replace").splitlines()
                    ):
                        return DetectResult(0.95, "timestamped bash history")
        except (OSError, ValueError):
            pass
        return DetectResult(0.0, "no timestamped shell history")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Event]:
        redactions = 0
        malformed = 0
        with _sources(path) as sources:
            for name, stream in sources:
                basename = Path(name).name
                if basename == "history.db":
                    database = path / name if path.is_dir() else path
                    if path.is_file() and is_zipfile(path):
                        with tempfile.TemporaryDirectory(prefix="sherd-atuin-") as directory:
                            database = Path(directory) / "history.db"
                            with database.open("wb") as output:
                                shutil.copyfileobj(stream, output, length=65536)
                            yield from self._atuin(database, name)
                    else:
                        yield from self._atuin(database, name)
                    continue
                shell = "zsh" if basename == ".zsh_history" else "bash"
                occurrences: Counter[str] = Counter()
                for epoch, duration, command in _text_records(stream, shell):
                    try:
                        ts = _EPOCH + timedelta(seconds=epoch)
                    except (OverflowError, ValueError):
                        malformed += 1
                        continue
                    if not command.strip():
                        malformed += 1
                        continue
                    identity = content_hash(shell, epoch, command)
                    occurrence = occurrences[identity]
                    occurrences[identity] += 1
                    safe, count = redact(command)
                    redactions += count
                    yield Event(
                        source_file=name,
                        source_row_id=content_hash(shell, epoch, command, occurrence),
                        ts=ts,
                        kind="shell.command",
                        title=safe,
                        meta={"shell": shell, "duration_ms": duration, "exit": None},
                    )
        _LOG.info("shell history redactions=%d malformed=%d", redactions, malformed)

    def _atuin(self, database: Path, name: str) -> Iterator[Event]:
        redactions = 0
        malformed = 0
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            for row in conn.execute(
                "SELECT id, timestamp, duration, exit, command, cwd "
                "FROM history WHERE deleted_at IS NULL ORDER BY rowid"
            ):
                row_id, epoch, duration, exit_code, command, cwd = row
                try:
                    if not row_id or command is None or not str(command).strip():
                        raise ValueError("missing required field")
                    ts = _EPOCH + timedelta(microseconds=int(epoch) // 1000)
                    duration_ms = int(duration) / 1_000_000 if duration is not None else None
                except (TypeError, ValueError, OverflowError):
                    malformed += 1
                    continue
                safe, count = redact(str(command))
                redactions += count
                home = str(Path.home())
                cwd = str(cwd) if cwd is not None else None
                if cwd == home or (cwd is not None and cwd.startswith(home + "/")):
                    cwd = "~" + cwd[len(home) :]
                # Exported home prefixes may belong to a different machine.
                if cwd is not None:
                    cwd = re.sub(r"^/(?:Users|home)/[^/]+(?=/|$)", "~", cwd)
                yield Event(
                    source_file=name,
                    source_row_id=str(row_id),
                    ts=ts,
                    kind="shell.command",
                    title=safe,
                    meta={
                        "shell": "atuin",
                        "duration_ms": duration_ms,
                        "exit": exit_code,
                        "cwd": cwd,
                    },
                )
        _LOG.info("shell history redactions=%d malformed=%d", redactions, malformed)

    def fixtures(self) -> list[Path]:
        root = Path(__file__).resolve().parents[5] / "fixtures" / self.id
        return sorted(path for path in root.iterdir() if (path / "meta.json").is_file())


CONNECTOR = ShellHistoryConnector()
