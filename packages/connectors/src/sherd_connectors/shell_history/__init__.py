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
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def unmetafy(data: bytes) -> str:
    """Unescape zsh's Meta byte before decoding UTF-8."""
    if b"\x83" in data:
        data = re.sub(rb"\x83(.)", lambda match: bytes([match[1][0] ^ 0x20]), data, flags=re.S)
    return data.decode("utf-8", errors="replace")


def _format(sample: bytes) -> str | None:
    if sample.startswith(b"SQLite format 3\x00"):
        return "atuin"
    lines = sample.decode("utf-8", "replace").splitlines()
    if any(_ZSH.fullmatch(line) for line in lines):
        return "zsh"
    if any(_BASH.fullmatch(line) for line in lines):
        return "bash"
    return None


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(history)")}


def _atuin_detect(database: Path) -> bool:
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            return {"id", "timestamp", "command"} <= _columns(conn)
    except sqlite3.DatabaseError:
        return False


@contextmanager
def _sources(path: Path) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    if path.is_file() and is_zipfile(path):
        with ZipFile(path) as archive:

            def members() -> Iterator[tuple[str, IO[bytes]]]:
                for name in sorted(archive.namelist()):
                    if not name.endswith("/"):
                        with archive.open(name) as stream:
                            yield name, stream

            yield members()
    else:

        def files() -> Iterator[tuple[str, IO[bytes]]]:
            candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
            for file in candidates:
                if file.is_file():
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
                try:
                    epoch, duration = int(match[1]), int(match[2]) * 1000
                except ValueError:
                    epoch, parts = None, []
                    skipped += 1
                    continue
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
                parts = []
                try:
                    epoch = int(match[1])
                except ValueError:
                    epoch = None
                    skipped += 1
            elif epoch is not None:
                parts.append(line)
            else:
                skipped += 1
    if parts and epoch is not None:
        yield epoch, duration, "\n".join(parts)
    _LOG.info("shell history skipped lines=%d", skipped)


class ShellHistoryConnector:
    id = "shell_history"
    version = "2"
    display_name = "Shell history"

    def detect(self, path: Path) -> DetectResult:
        try:
            with _sources(path) as sources:
                for index, (name, stream) in enumerate(sources):
                    if index == 3:
                        break
                    sample = stream.read(65536)
                    shell = _format(sample)
                    if shell == "atuin":
                        if is_zipfile(path):
                            # Validate schema from the bounded sample without extracting.
                            try:
                                with closing(sqlite3.connect(":memory:")) as conn:
                                    conn.deserialize(sample)
                                    if {"id", "timestamp", "command"} <= _columns(conn):
                                        return DetectResult(0.95, "atuin SQLite history")
                            except sqlite3.DatabaseError:
                                pass
                        elif _atuin_detect(path / name if path.is_dir() else path):
                            return DetectResult(0.95, "atuin SQLite history")
                    elif shell == "zsh":
                        return DetectResult(1.0, "zsh extended history")
                    elif shell == "bash":
                        return DetectResult(0.95, "timestamped bash history")
        except (OSError, ValueError):
            pass
        return DetectResult(0.0, "no timestamped shell history")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Event]:
        redactions = 0
        malformed = 0
        with _sources(path) as sources:
            for name, stream in sources:
                shell = _format(stream.read(65536))
                stream.seek(0)
                if shell is None:
                    continue
                if shell == "atuin":
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
                occurrences: Counter[str] = Counter()
                current_epoch: int | None = None
                for epoch, duration, command in _text_records(stream, shell):
                    if epoch != current_epoch:
                        occurrences.clear()
                        current_epoch = epoch
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
            columns = _columns(conn)
            if not {"id", "timestamp", "command"} <= columns:
                _LOG.info("shell history skipped schemas=1")
                return
            selected = ", ".join(
                column if column in columns else "NULL"
                for column in ("id", "timestamp", "duration", "exit", "command", "cwd")
            )
            predicate = " WHERE deleted_at IS NULL" if "deleted_at" in columns else ""
            for row in conn.execute(f"SELECT {selected} FROM history{predicate} ORDER BY rowid"):
                row_id, epoch, duration, exit_code, command, cwd = row
                try:
                    if not row_id or command is None or not str(command).strip():
                        raise ValueError("missing required field")
                    ts = _EPOCH + timedelta(microseconds=int(epoch) // 1000)
                    duration_ms = int(duration) // 1_000_000 if duration is not None else None
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
