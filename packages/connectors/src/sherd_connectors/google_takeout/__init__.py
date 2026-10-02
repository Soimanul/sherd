"""Google Takeout: watch activity, search activity and Chrome visits."""

import logging
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlsplit

import ijson  # type: ignore[import-untyped]  # ijson ships no typing metadata.
from sherd_core import Event, content_hash

from sherd_connectors.base import DetectResult, ImportContext
from sherd_connectors.google_takeout.dates import parse_date
from sherd_connectors.google_takeout.html import ActivityHTML

logger = logging.getLogger("sherd.connectors.google_takeout")
PREFIXES = (
    "Watched ",
    "Ați vizionat ",
    "Angesehen: ",
    "Has visto ",
    "Searched for ",
    "Ați căutat ",
    "Gesucht nach: ",
)
ADS = (
    "from google ads",
    "din google ads",
    "de la google ads",
    "aus google ads",
    "von google ads",
    "de google ads",
    "provenant de google ads",
)
SEARCH = ("search", "căutare", "google suche", "suche", "búsqueda", "recherche")


@contextmanager
def files(path: Path) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:

            def members() -> Iterator[tuple[str, IO[bytes]]]:
                for name in sorted(archive.namelist()):
                    if Path(name).suffix.lower() in (".json", ".html"):
                        with archive.open(name) as stream:
                            yield name, stream

            yield members()
    else:

        def members() -> Iterator[tuple[str, IO[bytes]]]:
            candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.is_file() and candidate.suffix.lower() in (".json", ".html"):
                    with candidate.open("rb") as stream:
                        yield (
                            candidate.relative_to(
                                path if path.is_dir() else path.parent
                            ).as_posix(),
                            stream,
                        )

        yield members()


def shape(prefix: bytes) -> str | None:
    text = prefix.decode("utf-8-sig", errors="replace")
    if "outer-cell" in text and "content-cell" in text:
        return "html"
    if re.search(r'"Browser History"\s*:', text):
        return "chrome"
    if text.lstrip().startswith("[") and all(
        re.search(r'"' + key + r'"\s*:', text) for key in ("header", "title", "time", "products")
    ):
        return "activity"
    return None


def activity(record: dict[str, Any], ctx: ImportContext, html: bool) -> Event | None:
    labels = [str(record.get("header", "")), *record.get("products", [])]
    label = " ".join(labels).lower()
    if "youtube" in label:
        kind = "youtube.watch"
    elif any(product in label for product in SEARCH):
        kind = "google.search"
    else:
        return None
    title = str(record.get("title") or "")
    if title.startswith(("Visited", "Besucht", "Ați accesat", "Visitado", "Consulté")):
        return None
    url = record.get("titleUrl")
    for prefix in PREFIXES:
        if title.startswith(prefix):
            title = title.removeprefix(prefix)
            break
    title = title.removesuffix(" angesehen")
    meta: dict[str, Any] | None = None
    if kind == "google.search":
        query = parse_qs(urlsplit(url or "").query, keep_blank_values=True).get("q")
        if query is not None:
            title = query[0]
    else:
        subtitles = record.get("subtitles", [])
        channel = subtitles[0] if subtitles else {}
        meta = {
            "channel_name": channel.get("name"),
            "channel_url": channel.get("url"),
            "from_ads": any(
                ad in str(detail.get("name", "")).lower()
                for detail in record.get("details", [])
                for ad in ADS
            ),
        }
        if title.startswith(("http://", "https://")) or "a video that has been removed" in title:
            title = ""
    ts = parse_date(str(record["time"]), ctx.tz) if html else datetime.fromisoformat(record["time"])
    if ts.tzinfo is None:
        raise ValueError("JSON activity timestamp must have an offset")
    return Event(
        source_file="pending",
        source_row_id="pending",
        ts=ts,
        kind=kind,
        title=title or None,
        url=url,
        meta=meta,
    )


class GoogleTakeout:
    id = "google_takeout"
    version = "1"
    display_name = "Google Takeout"

    def detect(self, path: Path) -> DetectResult:
        if not path.exists():
            return DetectResult(0.0, "no export")
        with files(path) as candidates:
            for index, (_, stream) in enumerate(candidates):
                if index == 5:
                    break
                if shape(stream.read(65536)):
                    return DetectResult(0.95, "Takeout activity structure")
        return DetectResult(0.0, "no Takeout activity structure")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Event]:
        skipped = 0
        with files(path) as candidates:
            for name, stream in candidates:
                prefix = stream.read(65536)
                format_ = shape(prefix)
                if format_ is None:
                    continue
                # ZipExtFile and local files both support seek without extracting.
                stream.seek(0)
                occurrences: Counter[str] = Counter()
                for record in records(stream, format_):
                    event: Event | None
                    try:
                        if format_ == "chrome":
                            event = Event(
                                source_file=name,
                                source_row_id="pending",
                                ts=datetime(1970, 1, 1, tzinfo=UTC)
                                + timedelta(microseconds=int(record["time_usec"])),
                                kind="chrome.visit",
                                title=record.get("title"),
                                url=record.get("url"),
                                meta={"page_transition": record.get("page_transition")},
                            )
                        else:
                            event = activity(record, ctx, format_ == "html")
                    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                        event = None
                    if event is None:
                        skipped += 1
                        continue
                    identity = content_hash(
                        event.kind, event.ts.isoformat(), event.url or event.title
                    )
                    occurrence = occurrences[identity]
                    occurrences[identity] += 1
                    yield event.model_copy(
                        update={
                            "source_file": name,
                            "source_row_id": content_hash(
                                event.kind,
                                event.ts.isoformat(),
                                event.url or event.title,
                                occurrence,
                            ),
                        }
                    )
        logger.info("activity skipped: count=%d", skipped)

    def fixtures(self) -> list[Path]:
        root = Path(__file__).resolve().parents[5] / "fixtures" / self.id
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def records(stream: IO[bytes], format_: str) -> Iterator[dict[str, Any]]:
    if format_ != "html":
        yield from ijson.items(stream, "Browser History.item" if format_ == "chrome" else "item")
    else:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        parser = ActivityHTML()
        while chunk := stream.read(4096):
            parser.feed(decoder.decode(chunk))
            while parser.records:
                yield parser.records.popleft()
        parser.feed(decoder.decode(b"", final=True))
        parser.close()
        yield from parser.records


CONNECTOR = GoogleTakeout()
