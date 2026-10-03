"""Bounded-memory parsing of WhatsApp chat exports."""

import logging
import os
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from io import TextIOWrapper
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, TextIO, cast
from zoneinfo import ZoneInfo

from sherd_core import Message, content_hash

from sherd_connectors.base import DetectResult, ImportContext

MediaType = Literal["image", "video", "audio", "document", "sticker", "gif", "other"]
DateOrder = Literal["dm", "md"]
logger = logging.getLogger("sherd.connectors.whatsapp")
_MARKS = dict.fromkeys(
    map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
)
_DATE = r"(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>\d{2}|\d{4})"
_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?(?:\s*(?P<ap>[ap]\.?\s?m\.?))?"
# Separate patterns avoid duplicate named groups in the two platform alternatives.
_ANDROID = re.compile(rf"^{_DATE},\s*{_TIME}\s+-\s+(?P<body>.*)$", re.IGNORECASE)
_IOS = re.compile(rf"^\[{_DATE},\s*{_TIME}\]\s*(?P<body>.*)$", re.IGNORECASE)
_PHONE = re.compile(r"^\+?[\d ()\-\.]+$")
# Group inference only: applied to already-classified system bodies, never senders.
_GROUP = re.compile(
    r"created (?:this |the )?group|added .+|left\.?$|"
    r"changed the (?:subject|icon|description|group)|"
    r"a creat grupul|a adăugat|a părăsit|a schimbat (?:subiectul|numele)|"
    r"gruppe .*(?:erstellt|gegründet)|hinzugefügt|hat .*verlassen|gruppenbetreff|"
    r"creó el grupo|añadió|salió del grupo|cambió el asunto",
    re.IGNORECASE,
)
# Sender-prefixed iOS notices require the marker and a whole-text match.
_SYSTEM = re.compile(
    r"(?:messages and calls are end-to-end encrypted(?:\.|"
    r"\. No one outside of this chat, not even WhatsApp, can read or listen to them\. "
    r"Tap to learn more\.)?|missed (?:voice|video) call\.?|"
    r"your security code with [^\n]+ changed\.?|security code changed\.?|"
    r"created (?:this |the )?group(?: [^\n]+)?|added [^\n]+|left\.?|"
    r"changed the (?:subject|(?:group )?icon|(?:group )?description)(?: [^\n]+)?|"
    r"mesajele și apelurile sunt criptate(?: integral)?\.?|"
    r"apel (?:vocal |video )?ratat\.?|codul (?:tău )?de securitate "
    r"cu [^\n]+ s-a schimbat\.?|a creat grupul(?: [^\n]+)?|a adăugat [^\n]+|"
    r"a părăsit(?: grupul)?\.?|a schimbat (?:subiectul|numele|pictograma|descrierea)"
    r"(?: [^\n]+)?|nachrichten und anrufe sind ende-zu-ende-verschlüsselt\.?|"
    r"verpasster (?:sprach|video)anruf\.?|sicherheitsnummer "
    r"mit [^\n]+ hat sich geändert\.?|gruppe(?: [^\n]+)? (?:erstellt|gegründet)\.?|"
    r"hinzugefügt(?: [^\n]+)?|hat (?:die gruppe )?verlassen\.?|"
    r"gruppen(?:betreff|bild|beschreibung) geändert(?: [^\n]+)?|"
    r"los mensajes y las llamadas están cifrados de extremo a extremo\.?|"
    r"llamada perdida\.?|cambió tu código de seguridad(?: con [^\n]+)?\.?|"
    r"creó el grupo(?: [^\n]+)?|añadió [^\n]+|salió del grupo\.?|"
    r"cambió (?:el asunto|el icono|la descripción)(?: [^\n]+)?)",
    re.IGNORECASE,
)
_DELETED = frozenset(
    phrase.casefold()
    for phrase in (
        "This message was deleted",
        "You deleted this message",
        "Acest mesaj a fost șters",
        "Ai șters acest mesaj",
        "Dieser Nachricht wurde gelöscht",
        "Diese Nachricht wurde gelöscht",
        "Du hast diese Nachricht gelöscht",
        "Este mensaje fue eliminado",
        "Eliminaste este mensaje",
        "Borraste este mensaje",
    )
)
_MEDIA = re.compile(
    r"^(?:<(?P<generic>Media omitted|Media lipsă|Fișier media omis|Medien ausgeschlossen|"
    r"Multimedia omitido)>|(?P<type>image|video|audio|sticker|GIF|document) omitted|"
    r"<attached:\s*(?P<ios>[^>]+)>|(?P<android>[^\n]+?) \(file attached\))"
    r"(?=$|\s)(?P<caption>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def clean(value: str) -> str:
    return value.translate(_MARKS)


def normalise_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def sender_id(sender: str) -> str:
    if _PHONE.fullmatch(sender) and 7 <= len(re.sub(r"\D", "", sender)) <= 15:
        return "whatsapp:+" + re.sub(r"\D", "", sender)
    return "whatsapp:name:" + sender


def is_self(sender: str, identities: frozenset[str]) -> bool:
    key = sender_id(sender)
    return any(sender_id(clean(identity).strip()) == key for identity in identities)


def marked_line(line: str) -> str:
    """Retain body controls until the leading iOS marker has been checked."""
    return line.lstrip("\ufeff" + "".join(chr(key) for key in _MARKS))


def prefix(line: str) -> re.Match[str] | None:
    return _ANDROID.match(line) or _IOS.match(line)


def detect_date_order(lines: Iterator[str]) -> DateOrder:
    """Unambiguous fields override the clock heuristic, across the entire chat."""
    dm = md = twelve = False
    for line in lines:
        match = prefix(clean(marked_line(line.rstrip("\r\n"))))
        if match:
            valid = False
            for candidate in ("dm", "md"):
                try:
                    wall_time(match, candidate)
                except ValueError:
                    continue
                valid = True
                break
            if not valid:
                continue
            dm |= int(match["a"]) > 12
            md |= int(match["b"]) > 12
            twelve |= match["ap"] is not None
    return "dm" if dm else "md" if md or twelve else "dm"


def wall_time(match: re.Match[str], order: DateOrder) -> datetime:
    a, b, year = int(match["a"]), int(match["b"]), int(match["y"])
    year = year + 2000 if year < 100 else year
    hour = int(match["h"])
    ampm = match["ap"]
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError("invalid WhatsApp 12-hour time")
        hour = hour % 12 + (12 if ampm.lower().startswith("p") else 0)
    return datetime(
        year,
        a if order == "md" else b,
        b if order == "md" else a,
        hour,
        int(match["m"]),
        int(match["s"] or 0),
    )


def timestamp(match: re.Match[str], order: DateOrder, tz: ZoneInfo) -> datetime:
    aware = wall_time(match, order).replace(tzinfo=tz, fold=0)
    # Round-trip shifts nonexistent wall times forward by the DST gap; fold=0
    # picks the first occurrence of an ambiguous wall time.
    return aware.astimezone(UTC).astimezone(tz)


@dataclass(frozen=True)
class ChatFile:
    path: Path
    member: str | None = None

    @property
    def name(self) -> str:
        return Path(self.member).name if self.member else self.path.name

    @contextmanager
    def open(self) -> Iterator[TextIO]:
        if self.member is None:
            with self.path.open(encoding="utf-8-sig", errors="replace") as stream:
                yield stream
        else:
            with (
                zipfile.ZipFile(self.path) as archive,
                archive.open(self.member) as raw,
                TextIOWrapper(raw, encoding="utf-8-sig", errors="replace") as stream,
            ):
                yield stream

    def sample(self) -> str:
        """Read at most the first 64 KiB, without text-buffer read-ahead."""
        if self.member is None:
            with self.path.open("rb") as raw:
                data = raw.read(65_536)
        else:
            with zipfile.ZipFile(self.path) as archive, archive.open(self.member) as raw:
                data = raw.read(65_536)
        return data.decode("utf-8-sig", errors="replace")

    def source_file(self, root: Path) -> str:
        relative = self.path.relative_to(root).as_posix()
        return relative if self.member is None else relative + "/" + self.member

    def chat_name(self) -> str | None:
        stem = Path(self.name).stem
        if stem.startswith("WhatsApp Chat with "):
            return clean(stem.removeprefix("WhatsApp Chat with ")).strip()
        if self.member and self.path.stem.startswith("WhatsApp Chat - "):
            return clean(self.path.stem.removeprefix("WhatsApp Chat - ")).strip()
        return None


def chat_files(path: Path) -> Iterator[ChatFile]:
    files = path.rglob("*") if path.is_dir() else iter([path])
    for file in files:
        if not file.is_file():
            continue
        if file.suffix.lower() == ".txt":
            yield ChatFile(file)
        elif file.suffix.lower() == ".zip":
            with zipfile.ZipFile(file) as archive:
                for member in sorted(archive.namelist()):
                    name = Path(member).name
                    if name == "_chat.txt" or (
                        name.startswith("WhatsApp Chat with ") and name.endswith(".txt")
                    ):
                        yield ChatFile(file, member)


def split_body(body: str) -> tuple[str | None, str]:
    sender, sep, text = body.partition(": ")
    # An empty body may be exported without the separator's trailing space.
    if not sep and body.endswith(":"):
        sender, sep, text = body[:-1], ":", ""
    if not sep:
        return None, clean(body)
    if text.startswith("\u200e") and _SYSTEM.fullmatch(clean(text).strip()):
        return None, clean(text)
    return clean(sender).strip(), clean(text)


def records(file: ChatFile) -> Iterator[tuple[re.Match[str], str | None, str]]:
    current: re.Match[str] | None = None
    sender: str | None = None
    parts: list[str] = []
    with file.open() as stream:
        for raw in stream:
            line = marked_line(raw.rstrip("\r\n"))
            match = prefix(line)
            if match:
                if current is not None:
                    yield current, sender, "\n".join(parts)
                current = match
                sender, body = split_body(match["body"])
                parts = [body]
            elif current is not None:
                parts.append(clean(line))
    if current is not None:
        yield current, sender, "\n".join(parts)


def media(text: str) -> tuple[MediaType, str | None] | None:
    match = _MEDIA.match(text)
    if match is None:
        return None
    kind: MediaType = "other"
    explicit = (match["type"] or "").lower()
    types: dict[str, MediaType] = {
        "image": "image",
        "video": "video",
        "audio": "audio",
        "sticker": "sticker",
        "gif": "gif",
        "document": "document",
    }
    if explicit in types:
        kind = types[explicit]
    else:
        filename = (match["ios"] or match["android"] or "").lower()
        ext = Path(filename).suffix
        if "sticker" in filename or ext == ".webp":
            kind = "sticker"
        elif ext == ".gif":
            kind = "gif"
        elif ext in (".jpg", ".jpeg", ".png", ".heic"):
            kind = "image"
        elif ext in (".mp4", ".mov", ".3gp"):
            kind = "video"
        elif ext in (".opus", ".ogg", ".mp3", ".m4a", ".wav"):
            kind = "audio"
        elif ext:
            kind = "document"
    return kind, match["caption"].strip() or None


Kind = Literal["text", "media", "system", "deleted"]
WallTime = tuple[int, int, int, int, int, int]
ParsedRecord = tuple[WallTime | None, str | None, str, Kind, str | None, MediaType | None, bool]


class RustParser(Protocol):
    def detect_date_order(self, source: str | BinaryIO) -> DateOrder: ...

    def RecordIterator(  # noqa: N802 — mirrors the native class constructor
        self, source: str | BinaryIO, order: DateOrder, *, prefixes: bool = False
    ) -> Iterator[list[ParsedRecord]]: ...


def parser_backend() -> RustParser | None:
    """Resolve per parse so the environment override also works in long-lived apps."""
    if os.environ.get("SHERD_WA") == "python":
        return None
    try:
        return cast(RustParser, import_module("sherd_wa"))
    except ModuleNotFoundError as exc:
        if os.environ.get("SHERD_WA") == "rust":
            raise RuntimeError("SHERD_WA=rust requires the sherd_wa extension") from exc
        return None
    except Exception as exc:
        if os.environ.get("SHERD_WA") == "rust":
            raise RuntimeError("SHERD_WA=rust could not import the sherd_wa extension") from exc
        logger.warning("sherd_wa import failed: %s", type(exc).__name__)
        return None


@contextmanager
def rust_source(file: ChatFile) -> Iterator[str | BinaryIO]:
    if file.member is None:
        yield str(file.path)
    else:
        with zipfile.ZipFile(file.path) as archive, archive.open(file.member) as raw:
            yield cast(BinaryIO, raw)


def file_date_order(file: ChatFile, backend: RustParser | None) -> DateOrder:
    if backend is not None:
        with rust_source(file) as source:
            return backend.detect_date_order(source)
    with file.open() as lines:
        return detect_date_order(iter(lines))


def message_records(
    file: ChatFile, order: DateOrder, backend: RustParser | None, *, prefixes: bool = False
) -> Iterator[ParsedRecord]:
    """Shared text → primitive message boundary, also used by the manual benchmark."""
    if backend is not None:
        with rust_source(file) as source:
            for batch in backend.RecordIterator(source, order, prefixes=prefixes):
                yield from batch
        return
    if prefixes:
        with file.open() as lines:
            for line in lines:
                match = prefix(marked_line(line.rstrip("\r\n")))
                if match is not None:
                    sender, body = split_body(match["body"])
                    yield primitive_record(match, sender, body, order, prefixes=True)
    else:
        for match, sender, body in records(file):
            yield primitive_record(match, sender, body, order)


def primitive_record(
    match: re.Match[str],
    sender: str | None,
    original: str,
    order: DateOrder,
    *,
    prefixes: bool = False,
) -> ParsedRecord:
    try:
        wall = wall_time(match, order)
        fields: WallTime | None = (
            wall.year,
            wall.month,
            wall.day,
            wall.hour,
            wall.minute,
            wall.second,
        )
    except ValueError:
        fields = None
    kind: Kind = "text"
    text = original or None
    media_type: MediaType | None = None
    if prefixes:
        text = None
    else:
        if sender is None:
            kind = "system"
        elif original.strip().removesuffix(".").casefold() in _DELETED:
            kind, text = "deleted", None
        elif attachment := media(original):
            kind = "media"
            media_type, text = attachment
    return (
        fields,
        sender,
        original,
        kind,
        text,
        media_type,
        sender is None and _GROUP.search(original) is not None,
    )


class WhatsAppConnector:
    id = "whatsapp"
    version = "3"
    display_name = "WhatsApp"

    def detect(self, path: Path) -> DetectResult:
        try:
            for file in chat_files(path):
                lines = file.sample().splitlines()
                matches = sum(prefix(clean(line.lstrip("\ufeff"))) is not None for line in lines)
                if matches:
                    named = file.name == "_chat.txt" or file.name.startswith("WhatsApp Chat with ")
                    return DetectResult(0.98 if named else 0.8, "WhatsApp timestamped chat text")
        except (OSError, UnicodeError, zipfile.BadZipFile):
            return DetectResult(0.0, "Unreadable chat export")
        return DetectResult(0.0, "No WhatsApp chat text")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Message]:
        backend = parser_backend()
        skipped = unnamed = emitted = 0
        for file in sorted(chat_files(path), key=lambda file: (file.path, file.member or "")):
            order = file_date_order(file, backend)
            senders: set[str] = set()
            group = False
            fallback: str | None = None
            # Only prefixes are needed for chat-wide metadata, not multiline text.
            for fields, sender, _body, _, _, _, is_group in message_records(
                file, order, backend, prefixes=True
            ):
                if fields is None:
                    continue
                group |= is_group
                if sender:
                    if len(senders) < 3:
                        senders.add(
                            "self" if is_self(sender, ctx.self_identities) else sender_id(sender)
                        )
                    if fallback is None and not is_self(sender, ctx.self_identities):
                        fallback = sender
            name = file.chat_name() or fallback
            chat_id = content_hash("whatsapp", normalise_name(name)) if name else None
            source_file = file.source_file(ctx.export_root)
            # The coordinator contract permits counters to reset at each timestamp.
            counts: dict[str, int] = {}
            previous_ts: str | None = None
            for fields, sender, original, kind, text, media_type, _ in message_records(
                file, order, backend
            ):
                if fields is None:
                    skipped += 1
                    continue
                aware = datetime(*fields, tzinfo=ctx.tz, fold=0)
                ts = aware.astimezone(UTC).astimezone(ctx.tz)
                if sender == "":
                    skipped += 1
                    continue
                if chat_id is None:
                    chat_id = content_hash(
                        "whatsapp", "unnamed", ts.astimezone(UTC).isoformat(), original
                    )
                    unnamed += 1
                timestamp_key = ts.isoformat()
                if timestamp_key != previous_ts:
                    counts.clear()
                    previous_ts = timestamp_key
                key = content_hash(chat_id, timestamp_key, sender, original)
                occurrence = counts.get(key, 0)
                counts[key] = occurrence + 1
                emitted += 1
                yield Message(
                    source_file=source_file,
                    source_row_id=content_hash(
                        chat_id, ts.isoformat(), sender, original, occurrence
                    ),
                    chat_id=chat_id,
                    chat_name=name,
                    chat_kind="group"
                    if name is not None and (group or len(senders) > 2)
                    else "direct",
                    sender_id=sender_id(sender) if sender is not None else None,
                    sender_name=sender,
                    is_from_me=sender is not None and is_self(sender, ctx.self_identities),
                    ts=ts,
                    text=text,
                    kind=kind,
                    media_type=media_type,
                )

        if not emitted:
            logger.warning("no records: connector=whatsapp count=0")

        if skipped:
            logger.warning("skipped malformed rows: connector=whatsapp count=%d", skipped)

        if unnamed:
            logger.warning("unnamed chats: connector=whatsapp count=%d", unnamed)

    def fixtures(self) -> list[Path]:
        for root in Path(__file__).resolve().parents:
            directory = root / "fixtures" / self.id
            if directory.is_dir():
                return sorted(
                    path for path in directory.iterdir() if (path / "meta.json").is_file()
                )
        return []
