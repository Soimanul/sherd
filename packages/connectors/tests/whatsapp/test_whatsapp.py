"""Behavioral contracts for local WhatsApp exports."""

import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.base import ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_connectors.testing import assert_streaming, export_path, load_meta
from sherd_connectors.whatsapp import CONNECTOR
from sherd_connectors.whatsapp.parser import clean, detect_date_order, normalise_name, sender_id
from sherd_connectors.whatsapp.synth_whatsapp import Locale, WhatsAppGenerator
from sherd_core import Message, Store, content_hash

VARIANTS = {path.name: path for path in CONNECTOR.fixtures()}


def parse_text(tmp_path: Path, text: str, *, name: str = "_chat.txt") -> list[Message]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    ctx = ImportContext(
        tmp_path, ZoneInfo("Europe/Bucharest"), frozenset({"Alex Demo", "+15550100"})
    )
    return list(CONNECTOR.parse(path, ctx))


def fixture_rows(variant: str) -> list[Message]:
    path = VARIANTS[variant]
    return list(CONNECTOR.parse(export_path(path), load_meta(path)))


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_detect_variants_and_confidence(variant: str) -> None:
    result = CONNECTOR.detect(export_path(VARIANTS[variant]))
    assert result.confidence == 0.98
    assert "Mira" not in result.reason


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("StreamingHistory.json", '[{"endTime":"2024-01-13 09:00","msPlayed":1000}]'),
        ("history.txt", ": 1705136400:0;echo synthetic"),
        ("bank.csv", "Date,Description,Amount\n2024-01-13,Paper moons,10.00\n"),
        ("watch-history.json", '[{"title":"Synthetic moon","time":"2024-01-13T09:00:00Z"}]'),
    ],
)
def test_detect_negative_other_formats(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.write_text(text)
    assert CONNECTOR.detect(path).confidence == 0.0


def test_detect_negative_other_connectors(tmp_path: Path) -> None:
    # This WP's baseline has no other connector fixtures yet; always exercise a
    # non-chat input, and check every other connector fixture when integrated.
    other = tmp_path / "unrelated.txt"
    other.write_text("synthetic non-chat export\n")
    candidates = [other]
    for root in Path("fixtures").iterdir():
        if root.name != "whatsapp" and root.is_dir():
            candidates.extend(export_path(p) for p in root.iterdir() if (p / "meta.json").is_file())
    for path in candidates:
        assert CONNECTOR.detect(path).confidence == 0.0


def test_detect_reads_only_first_64_kb(tmp_path: Path) -> None:
    path = tmp_path / "_chat.txt"
    path.write_text(" " * 65_536 + "\n[13/01/2024, 09:00:01] Mira Example: Hello\n")
    assert CONNECTOR.detect(path).confidence == 0.0


def test_empty_sender_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rows = parse_text(
        tmp_path, "13/01/2024, 09:00 - : Missing sender\n13/01/2024, 09:01 - Mira Example: Valid\n"
    )
    assert len(rows) == 1
    assert rows[0].text == "Valid"
    assert "count=1" in caplog.text
    assert "Missing sender" not in caplog.text


def test_detect_negative_and_generic_text(tmp_path: Path) -> None:
    path = tmp_path / "random.txt"
    path.write_text("Not a chat\n")
    assert CONNECTOR.detect(path).confidence == 0
    path.write_text("13/01/2024, 10:00 - Mira Example: Hello\n")
    assert CONNECTOR.detect(path).confidence == 0.8
    path.write_bytes(b"\xff\xfe")
    assert CONNECTOR.detect(path).confidence == 0
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"broken")
    assert CONNECTOR.detect(archive).confidence == 0


@pytest.mark.parametrize(
    ("lines", "order"),
    [
        (["13/01/24, 9:00 AM - A: x"], "dm"),
        (["01/13/24, 09:00 - A: x"], "md"),
        (["01/02/24, 9:00 AM - A: x"], "md"),
        (["01/02/24, 09:00 - A: x"], "dm"),
        (["[01/02/24, 9:00:01 a.m.] A: x"], "md"),
        (["01/02/24, 09:00 - A: x", "01/13/24, 09:01 - A: y"], "md"),
        (["01/02/24, 9:00 PM - A: x", "13/02/24, 9:01 PM - A: y"], "dm"),
        (["32/13/24, 09:00 - A: malformed", "01/02/24, 9:00 AM - A: x"], "md"),
    ],
)
def test_date_order_detection(lines: list[str], order: str) -> None:
    assert detect_date_order(iter(lines)) == order


@pytest.mark.parametrize("locale", ["en-US", "en-GB", "ro-RO", "de-DE"])
@pytest.mark.parametrize("ios", [False, True])
def test_synth_four_locales(tmp_path: Path, locale: Locale, ios: bool) -> None:
    path = tmp_path / "_chat.txt"
    generator = WhatsAppGenerator(locale, ios=ios)
    generator.write(path, 10_000, 4)
    initial = path.read_bytes()
    generator.write(path, 10_000, 4)
    assert path.read_bytes() == initial
    ctx = ImportContext(tmp_path, ZoneInfo("UTC"), frozenset())
    rows = list(CONNECTOR.parse(path, ctx))
    assert len(rows) >= 4
    assert rows[0].ts == datetime(2024, 1, 13, 8, tzinfo=UTC)
    assert rows[0].chat_name == "Mira Example"


def test_multiline_unicode_and_clock_variants() -> None:
    rows = fixture_rows("multiline-and-unicode")
    assert len(rows) == 3
    assert rows[0].text == (
        "First line\nSecond line 🙂\nA timestamp inside text: 1/13/24, 9:30 AM - still text"
    )
    assert rows[1].text == "Moon 🌙 مرحبا"
    assert rows[1].sender_name == "Mira Example"
    assert rows[1].ts.hour == 19
    assert rows[2].text == "Keep 👩🏽‍🚀 intact"
    assert rows[0].is_from_me
    assert not rows[1].is_from_me
    assert clean("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069🙂") == "🙂"


@pytest.mark.parametrize(
    ("placeholder", "kind"),
    [
        ("<Media omitted>", "other"),
        ("<Media lipsă>", "other"),
        ("<Fișier media omis>", "other"),
        ("<Medien ausgeschlossen>", "other"),
        ("<Multimedia omitido>", "other"),
        ("image omitted", "image"),
        ("video omitted", "video"),
        ("audio omitted", "audio"),
        ("sticker omitted", "sticker"),
        ("GIF omitted", "gif"),
        ("document omitted", "document"),
        ("<attached: 0001-PHOTO-2024-01-13.jpg>", "image"),
        ("IMG-20240113-WA0001.jpg (file attached)", "image"),
        ("<attached: 0002-VIDEO-2024-01-13.mp4>", "video"),
        ("<attached: 0003-AUDIO-2024-01-13.opus>", "audio"),
        ("<attached: 0004-STICKER-2024-01-13.webp>", "sticker"),
        ("notes.pdf (file attached)", "document"),
        ("<attached: moon.gif>", "gif"),
    ],
)
@pytest.mark.parametrize("caption", ["", "\n", "\nA paper moon 🌙", " A paper moon 🌙"])
def test_media_types_and_captions(
    tmp_path: Path, placeholder: str, kind: str, caption: str
) -> None:
    [row] = parse_text(tmp_path, f"13/01/2024, 09:00 - Mira Example: {placeholder}{caption}\n")
    assert row.kind == "media"
    assert row.media_type == kind
    assert row.text == (caption.strip() or None)
    assert row.reply_to_id is None


@pytest.mark.parametrize(
    "text",
    [
        "This message was deleted",
        "You deleted this message",
        "Acest mesaj a fost șters",
        "Ai șters acest mesaj",
        "Diese Nachricht wurde gelöscht",
        "Du hast diese Nachricht gelöscht",
        "Este mensaje fue eliminado",
        "Eliminaste este mensaje",
        "Borraste este mensaje",
    ],
)
def test_deleted_locales(tmp_path: Path, text: str) -> None:
    [row] = parse_text(tmp_path, f"13/01/2024, 09:00 - Mira Example: {text}\n")
    assert row.kind == "deleted"
    assert row.text is None
    assert row.media_type is None


@pytest.mark.parametrize(
    "text",
    [
        "Messages and calls are end-to-end encrypted.",
        "Alex Demo created group Paper Moons",
        "Alex Demo added Mira Example",
        "Mira Example left",
        'Alex Demo changed the subject to "Moon: Club"',
        "Missed voice call",
        "Your security code with Mira Example changed",
    ],
)
def test_system_lines(tmp_path: Path, text: str) -> None:
    [row] = parse_text(tmp_path, f"\u200e[13/01/2024, 09:00:01] {text}\n")
    assert row.kind == "system"
    assert row.sender_id is None
    assert row.sender_name is None
    assert row.is_from_me is False


def test_system_phrases_in_user_text_are_text(tmp_path: Path) -> None:
    [row] = parse_text(tmp_path, "13/01/2024, 09:00 - Mira Example: I added paper moons\n")
    assert row.kind == "text"


@pytest.mark.parametrize("variant", ["android-en-gb-group", "ios-ro-group"])
def test_group_system_messages(variant: str) -> None:
    assert all(row.chat_kind == "group" for row in fixture_rows(variant))


def test_group_sender_count_and_late_evidence(tmp_path: Path) -> None:
    text = "13/01/2024, 09:00 - Alex Demo: One\n13/01/2024, 09:01 - Mira Example: Two\n"
    assert all(row.chat_kind == "direct" for row in parse_text(tmp_path, text))
    assert all(
        row.chat_kind == "group"
        for row in parse_text(tmp_path, text + "13/01/2024, 09:02 - Theo Fiction: Three\n")
    )
    assert all(
        row.chat_kind == "group"
        for row in parse_text(
            tmp_path, text + "13/01/2024, 09:02 - Alex Demo created group Paper Moons\n"
        )
    )


def test_sender_and_chat_identity(tmp_path: Path) -> None:
    rows = parse_text(
        tmp_path,
        (
            "13/01/2024, 09:00 - Alex Demo: Hi\n"
            "13/01/2024, 09:01 - +1 (555) 0100: Me\n"
            "13/01/2024, 09:02 - Mira Example: Hi\n"
        ),
    )
    assert rows[0].sender_id == "whatsapp:name:Alex Demo"
    assert rows[1].sender_id == "whatsapp:+15550100"
    assert rows[0].is_from_me
    assert rows[1].is_from_me
    assert rows[0].chat_name == "Mira Example"
    assert rows[0].chat_id == content_hash("whatsapp", normalise_name("Mira Example"))
    assert sender_id("+1 555 0101") == sender_id("+15550101")
    assert normalise_name("  Mira   Example ") == "mira example"


def test_android_name_and_zip_name() -> None:
    assert fixture_rows("android-en-us")[0].chat_name == "Mira Example"
    ios = fixture_rows("ios-en")
    assert ios[0].chat_name == "Mira Example"
    assert ios[2].media_type == "image"
    assert ios[2].text == "A paper moon"
    assert ios[2].ts.second == 3
    assert ios[2].source_file == "WhatsApp Chat - Mira Example.zip/_chat.txt"


def test_directory_and_zip_without_extraction(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    text = "13/01/2024, 09:00 - Mira Example: Hello\n"
    (tmp_path / "WhatsApp Chat with Mira Example.txt").write_text(text)
    archive = tmp_path / "nested" / "WhatsApp Chat - Theo Fiction.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("_chat.txt", "[13/01/2024, 09:00:01] Theo Fiction: Hello\n")
        zipped.writestr("media.jpg", b"synthetic")
        zipped.writestr("notes.txt", "not chat text")
    ctx = ImportContext(tmp_path, ZoneInfo("UTC"), frozenset())
    rows = list(CONNECTOR.parse(tmp_path, ctx))
    assert {row.chat_name for row in rows} == {"Mira Example", "Theo Fiction"}
    assert len(rows) == 2
    assert not (tmp_path / "nested" / "_chat.txt").exists()
    assert CONNECTOR.detect(tmp_path).confidence == 0.98


def test_dst_gap_and_fold(tmp_path: Path) -> None:
    rows = parse_text(
        tmp_path,
        ("29/03/2026, 03:30 - Mira Example: Gap\n25/10/2026, 03:30 - Mira Example: Fold\n"),
    )
    assert rows[0].ts == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert rows[1].ts == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_identical_messages_have_occurrence_ids() -> None:
    rows = fixture_rows("edge-cases")
    same = [row for row in rows if row.text == "ok"]
    assert len(same) == 2
    assert same[0].source_row_id != same[1].source_row_id
    for index, row in enumerate(same):
        local = row.ts.astimezone(ZoneInfo("Europe/Bucharest"))
        assert row.source_row_id == content_hash(
            row.chat_id, local.isoformat(), "Mira Example", "ok", index
        )


def test_malformed_rows_skipped_and_count_only_log(caplog: pytest.LogCaptureFixture) -> None:
    rows = fixture_rows("edge-cases")
    assert len(rows) == 11
    assert rows[-1].text == "Valid after malformed records"
    assert len(caplog.records) == 1
    assert "count=2" in caplog.text
    assert "Mira" not in caplog.text
    assert "Invalid" not in caplog.text


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_run_import_idempotent(tmp_path: Path, variant: str) -> None:
    folder = VARIANTS[variant]
    with Store.open(tmp_path / "life.duckdb") as store:
        first = run_import(store, CONNECTOR, export_path(folder), load_meta(folder))
        second = run_import(store, CONNECTOR, export_path(folder), load_meta(folder))
        assert first.inserted == first.seen > 0
        assert second.inserted == 0
        assert second.seen == first.seen


def test_overlapping_export_import(tmp_path: Path) -> None:
    folder = VARIANTS["android-en-us"]
    source = export_path(folder)
    copied = tmp_path / "copied"
    shutil.copytree(source, copied)
    file = copied / "WhatsApp Chat with Mira Example.txt"
    with file.open("a") as stream:
        stream.write("1/13/24, 12:01 PM - Mira Example: Another paper moon\n")
    ctx = ImportContext(
        export_root(copied), ZoneInfo("Europe/Bucharest"), load_meta(folder).self_identities
    )
    with Store.open(tmp_path / "life.duckdb") as store:
        run_import(store, CONNECTOR, source, load_meta(folder))
        later = run_import(store, CONNECTOR, copied, ctx)
        assert later.inserted == 1


def test_streaming_50_mb(tmp_path: Path) -> None:
    path = tmp_path / "WhatsApp Chat with Mira Example.txt"
    WhatsAppGenerator().write(path, 50 * 1024**2, 4)
    assert path.stat().st_size >= 50 * 1024**2
    with path.open("rb") as stream:
        assert len(stream.readline()) < 200
    assert_streaming(CONNECTOR, path, max_rss_mb=200)


def test_unnamed_notes_to_self_identity(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    first = "[13/01/2024, 09:00:01] Alex Demo: Notes to self\nSecond line\n"
    rows = parse_text(tmp_path, first)
    [row] = rows
    assert row.chat_name is None
    assert row.chat_kind == "direct"
    assert row.chat_id == content_hash(
        "whatsapp", "unnamed", row.ts.isoformat(), "Notes to self\nSecond line"
    )
    assert "unnamed chats: connector=whatsapp count=1" in caplog.text
    assert "Notes" not in caplog.text
    extended = parse_text(tmp_path, first + "[13/01/2024, 09:01:02] Alex Demo: Another note\n")
    assert extended[0].chat_id == row.chat_id
    assert extended[0].source_row_id == row.source_row_id
    assert extended[1].chat_id == row.chat_id


def test_unnamed_system_only_chat(tmp_path: Path) -> None:
    [row] = parse_text(tmp_path, "[13/01/2024, 09:00:01] Messages and calls are encrypted.\n")
    assert row.chat_name is None
    assert row.chat_kind == "direct"
    assert row.kind == "system"
    assert row.chat_id == content_hash("whatsapp", "unnamed", row.ts.isoformat(), row.text)


def test_occurrence_reset_on_timestamp_change(tmp_path: Path) -> None:
    rows = parse_text(
        tmp_path,
        "13/01/2024, 09:00 - Mira Example: ok\n"
        "13/01/2024, 09:01 - Mira Example: Other\n"
        "13/01/2024, 09:00 - Mira Example: ok\n",
    )
    assert rows[0].source_row_id == rows[2].source_row_id
    ctx = ImportContext(tmp_path, ZoneInfo("Europe/Bucharest"), frozenset())
    with Store.open(tmp_path / "life.duckdb") as store:
        stats = run_import(store, CONNECTOR, tmp_path / "_chat.txt", ctx)
        assert stats.seen == 3
        assert stats.inserted == 2
