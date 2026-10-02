"""Spotify streaming-history connector; all timestamps are exported in UTC."""

import dbm
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal
from zoneinfo import ZoneInfo

import ijson  # type: ignore[import-untyped]  # ijson ships no type information.
from sherd_core import MediaPlay, content_hash

from sherd_connectors.base import DetectResult, ImportContext

logger = logging.getLogger("sherd.connectors.spotify")
_NAME = re.compile(
    r"(?:Streaming_History_(?:Audio|Video)_.+|StreamingHistory(?:_(?:music|podcast)_\d+|\d+))"
    r"\.json$"
)
_META = ("reason_start", "reason_end", "offline", "incognito_mode", "conn_country")
Kind = Literal["track", "episode", "video", "audiobook"]


def _supported(name: str) -> bool:
    return _NAME.fullmatch(PurePosixPath(name).name) is not None


def _order(name: str) -> tuple[bool, str]:
    return (not PurePosixPath(name).name.startswith("Streaming_History_"), name)


@contextmanager
def _files(path: Path, ctx: ImportContext) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    """Open only history members, without extracting archives."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = sorted(
                (m for m in archive.infolist() if not m.is_dir() and _supported(m.filename)),
                key=lambda m: _order(m.filename),
            )

            def zipped() -> Iterator[tuple[str, IO[bytes]]]:
                for member in members:
                    with archive.open(member) as stream:
                        yield (
                            path.relative_to(ctx.export_root).as_posix() + "/" + member.filename,
                            stream,
                        )

            yield zipped()
    else:
        files = sorted(
            (p for p in path.rglob("*.json") if _supported(p.name))
            if path.is_dir()
            else ([path] if _supported(path.name) else []),
            key=lambda p: _order(p.as_posix()),
        )

        def plain() -> Iterator[tuple[str, IO[bytes]]]:
            for file in files:
                with file.open("rb") as stream:
                    yield file.relative_to(ctx.export_root).as_posix(), stream

        yield plain()


def _text(record: dict[str, Any], field: str) -> str | None:
    value = record.get(field)
    return value if isinstance(value, str) and value else None


def _bool(record: dict[str, Any], field: str) -> bool | None:
    value = record.get(field)
    return value if isinstance(value, bool) else None


def _row(record: object, source_file: str) -> MediaPlay | None:
    if not isinstance(record, dict):
        return None
    extended = PurePosixPath(source_file).name.startswith("Streaming_History_")
    time = _text(record, "ts" if extended else "endTime")
    duration = record.get("ms_played" if extended else "msPlayed")
    if time is None or type(duration) is not int or duration < 0:
        return None
    try:
        ts = datetime.fromisoformat(time)
        ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    except ValueError:
        return None
    kind: Kind = "track"
    if extended:
        if any(
            _text(record, field)
            for field in (
                "audiobook_title",
                "audiobook_uri",
                "audiobook_chapter_uri",
                "audiobook_chapter_title",
            )
        ):
            kind = "audiobook"
        elif any(
            _text(record, field)
            for field in ("episode_name", "episode_show_name", "spotify_episode_uri")
        ):
            kind = "episode"
        elif PurePosixPath(source_file).name.startswith("Streaming_History_Video_"):
            kind = "video"
        artist = (
            _text(record, "master_metadata_album_artist_name")
            or _text(record, "episode_show_name")
            or _text(record, "audiobook_title")
        )
        track = (
            _text(record, "master_metadata_track_name")
            or _text(record, "episode_name")
            or _text(record, "audiobook_chapter_title")
        )
        uri = (
            _text(record, "spotify_track_uri")
            or _text(record, "spotify_episode_uri")
            or _text(record, "audiobook_chapter_uri")
        )
        meta = {field: record.get(field) for field in _META}
    else:
        if "podcast" in PurePosixPath(source_file).name or any(
            _text(record, field) for field in ("podcastName", "episodeName")
        ):
            kind = "episode"
        artist = _text(record, "artistName") or _text(record, "podcastName")
        track = _text(record, "trackName") or _text(record, "episodeName")
        uri, meta = None, None
    return MediaPlay(
        source_file=source_file,
        source_row_id="pending",
        ts=ts,
        media_kind=kind,
        artist=artist,
        track=track,
        album=_text(record, "master_metadata_album_album_name") if extended else None,
        uri=uri,
        ms_played=duration,
        platform=_text(record, "platform") if extended else None,
        shuffle=_bool(record, "shuffle") if extended else None,
        skipped=_bool(record, "skipped") if extended else None,
        meta=meta,
    )


class SpotifyConnector:
    id = "spotify"
    version = "1"
    display_name = "Spotify"

    def detect(self, path: Path) -> DetectResult:
        if not path.exists():
            return DetectResult(0.0, "No export found")
        ctx = ImportContext(path if path.is_dir() else path.parent, UTC_ZONE, frozenset())
        confidence = 0.0
        try:
            with _files(path, ctx) as files:
                for index, (_, stream) in enumerate(files):
                    if index >= 3:
                        break
                    prefix = stream.read(64 * 1024).lstrip()
                    if not prefix.startswith(b"["):
                        continue
                    confidence = max(confidence, 0.6)
                    if (b'"ts"' in prefix and b'"ms_played"' in prefix) or (
                        b'"endTime"' in prefix and b'"msPlayed"' in prefix
                    ):
                        return DetectResult(0.99, "Spotify history filename and fields")
        except (OSError, zipfile.BadZipFile):
            return DetectResult(0.0, "Unreadable export")
        return DetectResult(confidence, "Spotify history layout" if confidence else "No history")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[MediaPlay]:
        skipped = 0
        # Disk-backed hash-only bookkeeping bounds memory even for unsorted multi-year exports.
        # Per-file occurrence counts preserve legitimate repeats; emitted IDs collapse overlaps.
        with (
            tempfile.TemporaryDirectory(prefix="sherd-spotify-") as scratch,
            dbm.open(str(Path(scratch) / "seen"), "n") as seen,
            _files(path, ctx) as files,
        ):
            for source_file, stream in files:
                with dbm.open(str(Path(scratch) / "counts"), "n") as counts:
                    for record in ijson.items(stream, "item"):
                        row = _row(record, source_file)
                        if row is None:
                            skipped += 1
                            continue
                        fields = (
                            row.ts.replace(second=0, microsecond=0).isoformat(),
                            row.media_kind,
                            row.artist,
                            row.track,
                            row.ms_played,
                        )
                        key = content_hash(*fields).encode("ascii")
                        occurrence = int(counts.get(key, b"0"))
                        counts[key] = str(occurrence + 1).encode("ascii")
                        identity = content_hash(*fields, occurrence)
                        if identity.encode("ascii") in seen:
                            continue
                        seen[identity] = b"1"
                        yield row.model_copy(update={"source_row_id": identity})
        if skipped:
            logger.warning("parse skipped records: connector=spotify count=%d", skipped)

    def fixtures(self) -> list[Path]:
        root = Path(__file__).resolve().parents[5] / "fixtures" / self.id
        return sorted(p for p in root.iterdir() if (p / "meta.json").is_file())


UTC_ZONE = ZoneInfo("UTC")
CONNECTOR = SpotifyConnector()
