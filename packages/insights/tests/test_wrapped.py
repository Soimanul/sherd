"""Wrapped cards: six from the demo DB, byte-stable, private by default, skip rules, fonts."""

import hashlib
import json
import re
import struct
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sherd_connectors import synth
from sherd_core import MediaPlay, Store
from sherd_insights import wrapped
from sherd_insights.wrapped import WrappedCard, WrappedOptions

from .conftest import insert, message

SLOTS = ["numbers", "circle", "soundtrack", "rhythm", "money", "change"]
UTC = WrappedOptions(tz="UTC")


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    path = tmp_path_factory.mktemp("wrapped") / "demo.duckdb"
    with Store.open(path) as store:
        synth.import_profile(store, "demo")
    with Store.open(path, read_only=True) as store:
        yield store


def texts(card: WrappedCard) -> list[str]:
    """Every string drawn on the card."""
    found: list[str] = []
    for layer in card.spec["layer"]:
        value = layer["mark"].get("text") if isinstance(layer["mark"], dict) else None
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(value)
    return found


def size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def direct_contacts(store: Store) -> list[str]:
    rows = store.query(
        "SELECT DISTINCT chat_name FROM messages WHERE chat_kind = 'direct' "
        "UNION SELECT display_name FROM contacts"
    ).to_pylist()
    return [str(row["chat_name"]) for row in rows if row["chat_name"]]


def test_six_cards_from_the_demo_db(demo: Store) -> None:
    year = wrapped.default_year(demo, "UTC")
    assert year == 2025  # demo data runs Oct 2023 - Sep 2026: the last full year
    rendered = wrapped.render(demo, year, UTC)
    assert [card.key for card, _ in rendered] == SLOTS
    for number, (card, png) in enumerate(rendered, start=1):
        assert size(png) == (1080, 1350)
        assert card.spec["width"] == 1080
        assert card.spec["height"] == 1350
        assert f"{number} / 6" in texts(card)
        assert "made with sherd · nothing left my machine" in texts(card)
        assert card.alt
        assert any(layer["mark"].get("type") == "image" for layer in card.spec["layer"])


def test_same_db_year_and_options_give_identical_pngs(demo: Store) -> None:
    def hashes(options: WrappedOptions) -> list[str]:
        return [hashlib.sha256(png).hexdigest() for _, png in wrapped.render(demo, 2025, options)]

    first, second = hashes(UTC), hashes(UTC)
    assert first == second
    assert len(set(first)) == 6
    specs = [json.dumps(c.spec, sort_keys=True) for c in wrapped.build(demo, 2025, UTC)]
    assert specs == [json.dumps(c.spec, sort_keys=True) for c in wrapped.build(demo, 2025, UTC)]
    # The options change the pixels, so the cache key must include them.
    assert hashes(WrappedOptions(names=True, amounts=True, tz="UTC")) != first


def test_defaults_show_initials_and_no_amounts(demo: Store) -> None:
    cards = wrapped.build(demo, 2025, UTC)
    drawn = json.dumps([[*texts(card), card.alt, card.title] for card in cards])
    whole = json.dumps([card.spec for card in cards])
    for name in direct_contacts(demo):
        if " " in name:  # one-word names ("Mama") are their own initial, so check full names
            assert name not in whole, name
    assert "LO" in drawn  # Lavinia Owlcroft, the demo's top contact in 2025
    currency = demo.query("SELECT DISTINCT currency FROM transactions").to_pylist()
    for row in currency:
        assert row["currency"] not in drawn
    assert not re.search(r"\d[\d,]*\.\d\d", drawn), "a currency-style amount was drawn"


def test_flags_add_names_and_amounts(demo: Store) -> None:
    cards = wrapped.build(demo, 2025, WrappedOptions(names=True, amounts=True, tz="UTC"))
    drawn = json.dumps([[*texts(card), card.alt] for card in cards])
    assert "Lavinia Owlcroft" in drawn
    assert "RON" in drawn
    assert re.search(r"\d[\d,]*\.\d\d RON", drawn)


def test_cards_never_carry_message_text(demo: Store) -> None:
    whole = json.dumps(
        [c.spec for c in wrapped.build(demo, 2025, WrappedOptions(names=True, tz="UTC"))]
    )
    bodies = demo.query(
        "SELECT DISTINCT text FROM messages WHERE length(text) >= 12 LIMIT 5000"
    ).to_pylist()
    assert bodies
    assert not [row["text"] for row in bodies if row["text"] in whole]


def test_partial_previous_year_has_no_change_card(demo: Store) -> None:
    # 2023 starts in October, so a 2024-vs-2023 change would compare 12 months with 3.
    assert "change" not in [c.key for c in wrapped.build(demo, 2024, UTC)]
    assert wrapped.build(demo, 1999, UTC) == []


def test_missing_data_skips_cards_and_keeps_the_rest(store: Store) -> None:
    insert(
        store,
        [
            message("a1", "2024-03-02T09:00:00+00:00", contact="Ana Pop"),
            message("a2", "2024-03-02T09:05:00+00:00", contact="Ana Pop", me=False),
            message("b1", "2024-07-04T01:30:00+00:00", contact="Bob Stone"),
            message("end", "2024-12-31T20:00:00+00:00", contact="Ana Pop"),
        ],
    )
    assert wrapped.default_year(store, "UTC") == 2024
    rendered = wrapped.render(store, 2024, UTC)
    assert [card.key for card, _ in rendered] == ["numbers", "circle", "rhythm"]
    assert "3 / 3" in texts(rendered[-1][0])
    assert all(size(png) == (1080, 1350) for _, png in rendered)
    assert "Ana Pop" not in json.dumps([card.spec for card, _ in rendered])


def test_default_year_needs_the_last_day_of_a_year(store: Store) -> None:
    assert wrapped.default_year(store, "UTC") is None
    insert(store, [message("x", "2024-12-31T22:30:00+00:00")])
    assert wrapped.default_year(store, "UTC") == 2024
    # 22:30 UTC on 31 Dec is already 1 Jan in Bucharest: 2024 is the last full local year there.
    assert wrapped.default_year(store, "Europe/Bucharest") == 2024
    insert(store, [message("y", "2025-06-01T10:00:00+00:00")])
    assert wrapped.default_year(store, "UTC") == 2024
    assert wrapped.years(store, "UTC") == [2025, 2024]


def _play(key: str, ts: str, artist: str, track: str) -> MediaPlay:
    return MediaPlay(
        source_file="synthetic.json",
        source_row_id=key,
        ts=datetime.fromisoformat(ts),
        media_kind="track",
        artist=artist,
        track=track,
        ms_played=3_600_000,
    )


def test_long_names_stay_inside_the_card(store: Store) -> None:
    long_name = "Maximiliana Wolkenstein-Hohenberg von der Lindenstraße"
    insert(
        store,
        [
            message("a", "2024-05-01T10:00:00+00:00", contact=long_name),
            message("z", "2024-12-31T10:00:00+00:00", contact=long_name),
        ],
    )
    key = store.begin_import("synthetic_music", "1", "synthetic", "UTC")
    store.upsert(
        key,
        "synthetic_music",
        [
            _play(
                "p",
                "2024-05-01T10:00:00+00:00",
                "The Extraordinarily Long Named Orchestra of Faraway Hills",
                "An Even Longer Track Title That Keeps Going Well Past Any Sensible Width",
            )
        ],
    )
    store.finish_import(key, "succeeded")
    for card in wrapped.build(store, 2024, WrappedOptions(names=True, tz="UTC")):
        for layer in card.spec["layer"]:
            mark: dict[str, Any] = layer["mark"]
            if not isinstance(mark, dict) or mark.get("type") != "text":
                continue
            lines = mark["text"] if isinstance(mark["text"], list) else [mark["text"]]
            for line in lines:
                width = wrapped.measure(line, mark["fontSize"], mark["fontWeight"])
                left = {"left": mark["x"], "center": mark["x"] - width / 2}.get(
                    mark["align"], mark["x"] - width
                )
                assert left >= 0, (card.key, line)
                assert left + width <= 1080 - 40, (card.key, line)


def test_initials() -> None:
    assert wrapped.initials("Lavinia Owlcroft") == "LO"
    assert wrapped.initials("Ana Maria de la Cruz") == "AC"
    assert wrapped.initials("Mama") == "M"
    assert wrapped.initials("+1 555 0100") == "?"


def test_every_text_uses_the_registered_fira_sans(demo: Store) -> None:
    fonts = wrapped.font_dir()
    assert sorted(p.name for p in fonts.glob("*.ttf")) == sorted(wrapped.FONT_FILES.values())
    for card in wrapped.build(demo, 2025, UTC):
        assert card.spec["config"]["font"] == "Fira Sans"
        for layer in card.spec["layer"]:
            if layer["mark"].get("type") == "text":
                assert layer["mark"]["font"] == "Fira Sans"
                assert layer["mark"]["fontWeight"] in wrapped.FONT_FILES


PROBE = """
import hashlib, json, sys
import vl_convert
if sys.argv[1] == "register":
    vl_convert.register_font_directory(sys.argv[2])
spec = json.loads(sys.argv[3])
for font in ("Fira Sans", "No Such Font Sherd"):
    spec["layer"][0]["mark"]["font"] = font
    print(hashlib.sha256(vl_convert.vegalite_to_png(spec, allowed_base_urls=[])).hexdigest())
"""


def test_fira_sans_comes_from_the_registered_directory() -> None:
    """vl-convert draws Fira Sans only once our TTF directory is registered.

    Each case runs in a fresh process, because registrations last for the process. Without
    the directory "Fira Sans" falls back to the same face as a made-up family; with it, the
    pixels change. (Assumes Fira Sans is not installed system-wide, as on CI.)
    """
    spec = {
        "width": 400,
        "height": 120,
        "background": "#171412",
        "data": {"values": [{}]},
        "layer": [
            {
                "mark": {
                    "type": "text",
                    "x": 10,
                    "y": 80,
                    "text": "Your 2025, dug up",
                    "fontSize": 48,
                    "fontWeight": 600,
                    "align": "left",
                    "color": "#f3ebe3",
                }
            }
        ],
    }

    def probe(mode: str) -> list[str]:
        out = subprocess.run(
            [sys.executable, "-c", PROBE, mode, str(wrapped.font_dir()), json.dumps(spec)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert len(out) == 2
        return out

    bare_fira, bare_missing = probe("bare")
    fira, missing = probe("register")
    assert bare_fira == bare_missing  # unregistered: a fallback face
    assert fira != missing  # registered: a real Fira Sans
    assert fira != bare_fira
    assert missing == bare_missing


def test_render_png_registers_the_vendored_fonts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    wrapped._register_fonts.cache_clear()
    monkeypatch.setattr("vl_convert.register_font_directory", seen.append)
    card = WrappedCard("x", "x", "x", wrapped._spec([wrapped.text(10, 50, "sherd", 30)]))
    assert size(wrapped.render_png(card)) == (1080, 1350)
    assert seen == [str(wrapped.font_dir())]
    assert Path(seen[0]).name == "ttf"
    wrapped._register_fonts.cache_clear()


def test_numbers_card_counts_the_local_year(store: Store) -> None:
    insert(
        store,
        [
            message("in", "2024-01-01T00:30:00+00:00"),
            message("edge", "2023-12-31T22:30:00+00:00"),  # 2024 in Bucharest, 2023 in UTC
            message("end", "2024-12-31T12:00:00+00:00"),
        ],
    )
    utc = wrapped.build(store, 2024, UTC)[0]
    local = wrapped.build(store, 2024, WrappedOptions(tz="Europe/Bucharest"))[0]
    assert "2" in texts(utc)
    assert "3" in texts(local)
