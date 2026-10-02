"""Wrapped: a calendar year in six shareable cards (PLAN §6, §12.5, WP-18).

Each card is a 1080x1350 Vega-Lite spec laid out in pixels (text and simple marks over
the sherd theme), rendered to PNG by vl-convert with the vendored Fira Sans TTFs. Card
numbers come from the digs, so Wrapped agrees with the dashboard. Cards never carry
message text; contact names and currency amounts only appear when the options ask for
them. A card whose data is missing is skipped and the rest still render.

Same database, year and options give byte-identical PNGs: nothing here reads the clock
or a random source, and the decorative shapes come from fixed formulas.

The fonts and the owl mark are the web package's (`sherd_web/static/`), so the cards and
the UI share one copy; Wrapped needs `sherd-web` installed, as `sherd-cli` does.
"""

import base64
import importlib.util
import struct
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from functools import cache
from math import cos, pi, sin
from pathlib import Path
from typing import Any

import vl_convert
from sherd_core import Store

from sherd_insights import charts, registry
from sherd_insights.base import DigParams, DigResult
from sherd_insights.digs.cross_metrics import fact_tables

# ---- Privacy defaults (PLAN §12.5) -------------------------------------------------------------
# What a card may show unless the user asks for more. Change them here, and only here.
SHOW_NAMES = False  # circle card: contact initials only; True prints full names
SHOW_AMOUNTS = False  # money and change cards: shares and directions; True adds currency amounts
TOP_CONTACTS = 3  # people on the circle card; the layout has room for at most three
OTHER_CHANGES = 3  # smaller changes listed under the biggest one


@dataclass(frozen=True)
class WrappedOptions:
    names: bool = SHOW_NAMES
    amounts: bool = SHOW_AMOUNTS
    tz: str = "UTC"


@dataclass(frozen=True)
class WrappedCard:
    """One card: its slot key, a title, a text alternative and the Vega-Lite spec."""

    key: str
    title: str
    alt: str
    spec: dict[str, Any]


class WrappedError(RuntimeError):
    """Wrapped cannot render on this install (fonts or owl mark missing)."""


# ---- Canvas and palette ------------------------------------------------------------------------
# Colours mirror sherd_web/static/tokens.css, like the chart theme does: vl-convert cannot read CSS.
WIDTH, HEIGHT = 1080, 1350
MARGIN = 96
RIGHT = WIDTH - MARGIN
CONTENT = RIGHT - MARGIN
GROUND = 1150  # where the strata start; content stays above it

BG = "#171412"  # --color-bg
SURFACE_1 = "#1f1c19"
SURFACE_2 = "#282421"
SURFACE_3 = "#312d29"
LINE = "#403b37"
LINE_STRONG = "#817a75"
TEXT = "#f3ebe3"
TEXT_2 = "#ccc2ba"
TEXT_3 = "#aea49c"
ACCENT = "#cd6b44"
ACCENT_TEXT = "#ec9668"
ACCENT_SOFT = "#41261b"
ON_ACCENT = "#130e0c"
CLAY_DEEP = "#8c4a2e"  # between --color-accent and --color-accent-soft; text on it is TEXT
MUTED_BAR = "#5b524c"  # quiet marks next to an accent one (≥ 3:1 against BG is not needed)
UP = "#cf6139"  # --chart-cat-1, as trends.yoy colours increases
DOWN = "#0c9485"  # --chart-cat-2, decreases
STRATA = ("#1e1a17", "#241e1a", "#2b231e")  # sediment, lightest at the bottom

FONT = "Fira Sans"
FONT_FILES = {400: "FiraSans-Regular.ttf", 500: "FiraSans-Medium.ttf", 600: "FiraSans-SemiBold.ttf"}
WATERMARK = "made with sherd · nothing left my machine"
MINUS = "\u2212"  # a real minus sign
MONTHS = "JFMAMJJASOND"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# A broken rim fragment in Vega's unit symbol square, and the glaze line across it.
SHERD = (
    "M-1,-0.1L-0.62,-0.6L-0.12,-0.46L0.3,-0.88L0.96,-0.4L0.8,0.28L0.24,0.5L-0.3,0.86L-0.82,0.44Z"
)
GLAZE = "M-0.86,-0.02Q0.05,-0.42 0.86,-0.22"
ARROW_UP = "M0,-1L0.78,-0.12L0.26,-0.12L0.26,1L-0.26,1L-0.26,-0.12L-0.78,-0.12Z"
ARROW_FLAT = "M1,0L0.12,0.78L0.12,0.26L-1,0.26L-1,-0.26L0.12,-0.26L0.12,-0.78Z"

# Friendlier names for trends.yoy rows, by dig id. Labels from the dig itself can hold a
# contact or track name, so a dig without an entry falls back to its title, never its label.
CHANGE_NAMES: dict[str, tuple[str, str]] = {
    "messages.activity_heatmap": (
        "Night-owl index",
        "Share of your messages sent midnight to 5 am",
    ),
    "messages.conversation_starters": (
        "Conversations you started",
        "Share of chats you opened after six quiet hours",
    ),
    "messages.response_times": ("Your reply time", "Median minutes before you answered"),
    "messages.streaks_silences": ("Longest streak", "Days in a row talking to the same person"),
    "messages.volume_by_contact": ("Your top chat", "Messages with the person you talked to most"),
    "money.delivery_index": ("Food delivery", "Delivery's share of December's spending"),
    "money.income_vs_spend": ("Savings rate", "Average share of monthly income you kept"),
    "money.spend_by_category": ("Biggest spending category", "What you spent on it"),
    "money.subscriptions": ("Subscriptions", "Yearly cost of the ones still running"),
    "money.weekday_weekend": ("Weekend premium", "How much more a weekend day cost than a weekday"),
    "music.discovery_rate": ("New artists", "Share of plays from artists heard for the first time"),
    "music.listening_minutes": ("Listening time", "Hours of music and podcasts"),
    "music.skips_obsessions": ("Biggest obsession", "Plays of one track in a single week"),
}


# ---- Fonts and assets --------------------------------------------------------------------------


def static_dir() -> Path:
    """`sherd_web/static/`, found without importing the web app."""
    spec = importlib.util.find_spec("sherd_web")
    if spec is None or not spec.submodule_search_locations:
        raise WrappedError("Wrapped needs the sherd-web package for its fonts and owl mark.")
    return Path(next(iter(spec.submodule_search_locations))) / "static"


def font_dir() -> Path:
    path = static_dir() / "fonts" / "ttf"
    missing = [name for name in FONT_FILES.values() if not (path / name).is_file()]
    if missing:
        raise WrappedError(f"Wrapped fonts are missing from {path}: {', '.join(missing)}")
    return path


_render_lock = threading.Lock()


@cache
def _register_fonts(path: str) -> None:
    # vl-convert ignores woff2 and keeps registrations for the life of the process.
    vl_convert.register_font_directory(path)


def render_png(card: WrappedCard) -> bytes:
    """The card as PNG bytes, drawn with the vendored Fira Sans and no external requests."""
    with _render_lock:
        _register_fonts(str(font_dir()))
        return vl_convert.vegalite_to_png(card.spec, scale=1, allowed_base_urls=[])


@cache
def _owl_url(color: str) -> str:
    """The owl mark as a data URL, its `currentColor` fixed to `color`."""
    svg = (static_dir() / "owl-mark.svg").read_text(encoding="utf-8")
    start, end = svg.find("<style>"), svg.find("</style>")
    if start != -1 and end != -1:
        svg = svg[:start] + svg[end + len("</style>") :]
    svg = svg.replace("currentColor", color)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")


class _Metrics:
    """Advance widths from a TrueType file (cmap format 4 and hmtx), for line breaking."""

    def __init__(self, path: Path) -> None:
        data = path.read_bytes()
        tables: dict[str, int] = {}
        for index in range(struct.unpack_from(">H", data, 4)[0]):
            tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + 16 * index)
            tables[tag.decode("latin-1")] = offset
        self.units = int(struct.unpack_from(">H", data, tables["head"] + 18)[0])
        count = struct.unpack_from(">H", data, tables["hhea"] + 34)[0]
        advances = [struct.unpack_from(">H", data, tables["hmtx"] + 4 * i)[0] for i in range(count)]
        self.widths: dict[str, int] = {}
        cmap = tables["cmap"]
        for index in range(struct.unpack_from(">H", data, cmap + 2)[0]):
            platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * index)
            table = cmap + offset
            if (platform, encoding) in ((3, 1), (0, 3)) and struct.unpack_from(">H", data, table)[
                0
            ] == 4:
                break
        else:
            raise WrappedError(f"{path.name} has no Unicode BMP character map")
        segments = struct.unpack_from(">H", data, table + 6)[0] // 2
        ends = table + 14
        starts = ends + 2 * segments + 2
        deltas = starts + 2 * segments
        ranges = deltas + 2 * segments
        for s in range(segments):
            end, start = (
                struct.unpack_from(">H", data, base + 2 * s)[0] for base in (ends, starts)
            )
            delta = struct.unpack_from(">h", data, deltas + 2 * s)[0]
            range_offset = struct.unpack_from(">H", data, ranges + 2 * s)[0]
            for code in range(start, min(end, 0xFFFE) + 1):
                if range_offset:
                    address = ranges + 2 * s + range_offset + 2 * (code - start)
                    glyph = struct.unpack_from(">H", data, address)[0]
                    glyph = (glyph + delta) & 0xFFFF if glyph else 0
                else:
                    glyph = (code + delta) & 0xFFFF
                if glyph:
                    self.widths[chr(code)] = advances[min(glyph, count - 1)]
        self.fallback = self.widths.get("n", self.units // 2)

    def width(self, text: str, size: float) -> float:
        return sum(self.widths.get(ch, self.fallback) for ch in text) * size / self.units


@cache
def _metrics(weight: int) -> _Metrics:
    return _Metrics(font_dir() / FONT_FILES[weight])


def measure(text: str, size: float, weight: int = 400) -> float:
    return _metrics(weight).width(text, size)


def wrap(text: str, size: float, width: float, weight: int = 400, lines: int = 2) -> list[str]:
    """Greedy line breaks within `width`; the last allowed line ends in an ellipsis if cut."""
    out: list[str] = []
    for word in text.split():
        if out and measure(out[-1] + " " + word, size, weight) <= width:
            out[-1] += " " + word
        else:
            out.append(word)
    if len(out) > lines:
        out = [*out[: lines - 1], " ".join(out[lines - 1 :])]
    if out and measure(out[-1], size, weight) > width:
        last = out[-1]
        while last and measure(last.rstrip() + "…", size, weight) > width:
            last = last[:-1]
        out[-1] = last.rstrip() + "…"
    return out or [""]


# ---- Marks -------------------------------------------------------------------------------------
# Every element is a layer positioned in pixels; `scale: None` keeps data values as pixels.

Layer = dict[str, Any]
PIXEL: dict[str, Any] = {"type": "quantitative", "scale": None}


def text(
    x: float,
    y: float,
    value: str | list[str],
    size: float,
    *,
    weight: int = 400,
    color: str = TEXT,
    align: str = "left",
    baseline: str = "alphabetic",
    line_height: float | None = None,
) -> Layer:
    mark: dict[str, Any] = {
        "type": "text",
        "x": round(x, 2),
        "y": round(y, 2),
        "text": value,
        "font": FONT,
        "fontSize": size,
        "fontWeight": weight,
        "color": color,
        "align": align,
        "baseline": baseline,
    }
    if isinstance(value, list):
        mark["lineHeight"] = line_height or round(size * 1.08)
    return {"mark": mark}


def rect(
    x: float, y: float, x2: float, y2: float, color: str, *, radius: float = 0, **extra: Any
) -> Layer:
    return {
        "mark": {
            "type": "rect",
            "x": round(x, 2),
            "y": round(y, 2),
            "x2": round(x2, 2),
            "y2": round(y2, 2),
            "color": color,
            "cornerRadius": radius,
            "strokeWidth": 0,
            **extra,
        }
    }


def rects(values: list[dict[str, Any]], *, radius: float = 0) -> Layer:
    """Many rectangles in one layer: rows with x, x2, y, y2 and color."""
    return {
        "data": {"values": values},
        "mark": {"type": "rect", "cornerRadius": radius, "strokeWidth": 0},
        "encoding": {
            "x": {"field": "x", **PIXEL},
            "x2": {"field": "x2"},
            "y": {"field": "y", **PIXEL},
            "y2": {"field": "y2"},
            "color": {"field": "color", "type": "nominal", "scale": None},
        },
    }


def shape(
    x: float,
    y: float,
    path: str,
    width: float,
    color: str,
    *,
    angle: float = 0,
    filled: bool = True,
    stroke_width: float = 0,
) -> Layer:
    mark: dict[str, Any] = {
        "type": "point",
        "shape": path,
        "x": round(x, 2),
        "y": round(y, 2),
        "size": round(width * width),
        "angle": angle,
        "opacity": 1,
        "filled": filled,
    }
    if filled:
        mark.update({"color": color, "strokeWidth": 0})
    else:
        mark.update({"stroke": color, "strokeWidth": stroke_width, "strokeCap": "round"})
    return {"mark": mark}


def circle(x: float, y: float, radius: float, color: str) -> Layer:
    return shape(x, y, "circle", radius * 2, color)


def hairline(y: float, x: float = MARGIN, x2: float = RIGHT) -> Layer:
    return rect(x, y, x2, y + 1, LINE)


def stratum_line(y: float, phase: float) -> Layer:
    """A hairline that undulates like a boundary between two layers of soil."""
    points = [
        {"x": x, "y": round(y + 3.5 * sin(x / 61 + phase) + 2 * sin(x / 23 + phase * 2.1), 2)}
        for x in range(MARGIN, RIGHT + 1, 12)
    ]
    return {
        "data": {"values": points},
        "mark": {"type": "line", "color": LINE, "strokeWidth": 1.5, "interpolate": "monotone"},
        "encoding": {"x": {"field": "x", **PIXEL}, "y": {"field": "y", **PIXEL}},
    }


# ---- Frame: folio, strata, buried sherd, watermark ---------------------------------------------


def _strata(seed: int) -> list[Layer]:
    layers: list[Layer] = []
    for depth, (top, color) in enumerate(zip((GROUND, 1190, 1228), STRATA, strict=True)):
        phase = seed * 1.7 + depth * 2.3
        points = [
            {
                "x": x,
                "y": round(
                    top + 7 * sin(x / 97 + phase) + 4 * sin(x / 41 + phase * 1.9) - depth * 2, 2
                ),
                "y2": HEIGHT,
            }
            for x in range(-24, WIDTH + 48, 24)
        ]
        layers.append(
            {
                "data": {"values": points},
                "mark": {
                    "type": "area",
                    "color": color,
                    "fillOpacity": 1,
                    "line": False,
                    "interpolate": "monotone",
                },
                "encoding": {
                    "x": {"field": "x", **PIXEL},
                    "y": {"field": "y", **PIXEL},
                    "y2": {"field": "y2"},
                },
            }
        )
    return layers


# Where each card buries its sherd: x, y, rotation; fixed so renders repeat exactly.
FINDS = ((896, 1236, -18), (930, 1268, 24), (872, 1252, 140), (918, 1232, -62), (948, 1262, 96))


def _frame(seed: int, year: int, number: int, total: int) -> list[Layer]:
    x, y, angle = FINDS[seed % len(FINDS)]
    scale_left, segment = 200, 30
    segments = [
        {
            "x": scale_left + i * segment,
            "x2": scale_left + (i + 1) * segment,
            "y": 131,
            "y2": 139,
            "color": ACCENT if i % 5 == 4 else TEXT_2 if i % 2 == 0 else SURFACE_1,
        }
        for i in range(10)
    ]
    folio = f"{number} / {total}"
    folio_left = RIGHT - measure(folio, 26)
    return [
        *_strata(seed),
        shape(x, y, SHERD, 76, ACCENT, angle=angle),
        shape(x, y, GLAZE, 76, ACCENT_TEXT, angle=angle, filled=False, stroke_width=3),
        text(MARGIN, 145, str(year), 30, weight=500, color=ACCENT_TEXT),
        rect(scale_left - 1, 130, scale_left + 10 * segment + 1, 140, LINE_STRONG),
        rects(segments),
        rect(scale_left + 10 * segment + 20, 135, folio_left - 20, 136, LINE),
        text(RIGHT, 145, folio, 26, color=TEXT_3, align="right"),
        {
            "mark": {
                "type": "image",
                "url": _owl_url(TEXT_2),
                "x": MARGIN,
                "y": 1252,
                "width": 44,
                "height": 44,
                "align": "left",
                "baseline": "top",
            }
        },
        text(MARGIN + 60, 1283, WATERMARK, 26, color=TEXT_2),
    ]


def _title(value: str) -> tuple[list[Layer], float]:
    """The card title, up to two lines; returns the layers and the y below it."""
    lines = wrap(value, 76, CONTENT, 600)
    return [text(MARGIN, 290, lines, 76, weight=600, line_height=84)], 290 + 84 * (len(lines) - 1)


def _spec(layers: list[Layer]) -> dict[str, Any]:
    config = charts.load_theme()
    config["view"] = {**config.get("view", {}), "stroke": None}
    return {
        "$schema": charts.SCHEMA,
        "width": WIDTH,
        "height": HEIGHT,
        "padding": 0,
        "autosize": {"type": "none"},
        "background": BG,
        "config": config,
        "data": {"values": [{}]},
        "layer": layers,
    }


# ---- Formatting --------------------------------------------------------------------------------


def initials(name: str) -> str:
    """'Lavinia Owlcroft' -> 'LO'. Names without letters become '?'."""
    letters = [word[0].upper() for word in name.split() if word[0].isalpha()]
    if not letters:
        return "?"
    return letters[0] + letters[-1] if len(letters) > 1 else letters[0]


def count(value: float) -> str:
    return f"{round(value):,}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def amount(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def signed(value: float, unit: str) -> str:
    """'+122%' or '-8.6 pts' (with a real minus sign); whole numbers once a change is large."""
    digits = 0 if abs(value) >= 10 else 1
    body = f"{abs(value):.{digits}f}"
    sign = "+" if value > 0 else MINUS if value < 0 else ""
    return f"{sign}{body}{'%' if unit == '%' else ' pts'}"


def duration(hours: float) -> str:
    if hours < 1:
        minutes = round(hours * 60)
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    whole = round(hours)
    return f"{whole:,} hour" + ("" if whole == 1 else "s")


def clock(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    return f"{hour % 12} {'am' if hour < 12 else 'pm'}"


# ---- Data --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Year:
    store: Store
    year: int
    options: WrappedOptions

    @property
    def params(self) -> DigParams:
        return DigParams(
            date_from=date(self.year, 1, 1), date_to=date(self.year, 12, 31), tz=self.options.tz
        )

    def dig(self, dig_id: str, **changes: Any) -> DigResult | None:
        """A dig over the year, or None when it is not installed or has no rows."""
        dig = registry.discover().get(dig_id)
        counts = self.store.table_counts()
        if dig is None or any(not counts.get(table) for table in dig.requires):
            return None
        result = dig.compute(self.store, replace(self.params, **changes))
        return result if result.data.num_rows else None

    def rows(self, table: str, where: str = "TRUE") -> int:
        if not self.store.table_counts().get(table):
            return 0
        return int(
            self.store.query(
                f"SELECT count(*) AS n FROM {table} WHERE {where} "
                "AND timezone(?, ts)::DATE BETWEEN ? AND ?",
                [self.options.tz, date(self.year, 1, 1), date(self.year, 12, 31)],
            ).to_pylist()[0]["n"]
        )


def years(store: Store, tz: str) -> list[int]:
    """Local calendar years with any rows, newest first."""
    tables = [t for t in fact_tables(store) if store.table_counts().get(t)]
    if not tables:
        return []
    union = " UNION ".join(
        f"SELECT DISTINCT year(timezone(?, ts)) AS year FROM {table}" for table in tables
    )
    rows = store.query(f"SELECT year FROM ({union}) ORDER BY year DESC", [tz] * len(tables))
    return [int(row["year"]) for row in rows.to_pylist()]


def default_year(store: Store, tz: str) -> int | None:
    """The last full local year in the data, the rule trends.yoy uses."""
    tables = [t for t in fact_tables(store) if store.table_counts().get(t)]
    if not tables:
        return None
    union = " UNION ALL ".join(f"SELECT max(ts) AS ts FROM {table}" for table in tables)
    latest = store.query(
        f"SELECT max(timezone(?, ts)::DATE) AS day FROM ({union})", [tz]
    ).to_pylist()[0]["day"]
    if latest is None:
        return None
    return int(latest.year if (latest.month, latest.day) == (12, 31) else latest.year - 1)


# ---- Cards -------------------------------------------------------------------------------------
# Each builder returns (title, alt, layers) or None when its data is missing.

Built = tuple[str, str, list[Layer]]


def _numbers(y: _Year) -> Built | None:
    messages = y.rows("messages", "kind IN ('text', 'media')")
    listening = y.dig("music.listening_minutes", granularity="year")
    hours = float(listening.headline.value) if listening and listening.headline else 0.0
    transactions = y.rows("transactions")
    events = y.rows("events")
    facts = [
        (count(messages), "messages sent and received", messages),
        (count(hours) if hours >= 1 else "<1", "hours of music and podcasts", hours),
        (count(transactions), "payments, transfers and top-ups", transactions),
        (count(events), "other traces you left: searches, videos, visits", events),
    ]
    facts = [(big, label, n) for big, label, n in facts if n]
    if not facts:
        return None
    title = f"Your {y.year}, dug up"
    layers, top = _title(title)
    row = min(200, (GROUND - 40 - (top + 40)) / len(facts))
    for index, (big, label, _) in enumerate(facts):
        y0 = top + 40 + index * row
        if index:
            layers.append(stratum_line(y0 + 4, index * 1.3))
        layers.append(text(MARGIN, y0 + 118, big, 116, weight=600))
        layers.append(text(MARGIN, y0 + 170, label, 32, color=TEXT_2))
    alt = f"{y.year} in numbers: " + "; ".join(f"{big} {label}" for big, label, _ in facts) + "."
    return title, alt, layers


def _circle(y: _Year) -> Built | None:
    result = y.dig("messages.volume_by_contact", top_n=TOP_CONTACTS, granularity="year")
    if result is None:
        return None
    totals: dict[str, int] = {}
    for row in result.data.to_pylist():
        totals[str(row["contact"])] = totals.get(str(row["contact"]), 0) + int(row["messages"])
    people = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:TOP_CONTACTS]
    everything = y.rows("messages", "kind IN ('text', 'media')") or 1
    title = "Your circle"
    layers, top = _title(title)
    lede = (
        "The person you messaged most"
        if len(people) == 1
        else f"The {['', '', 'two', 'three', 'four', 'five'][len(people)]} people you messaged most"
    )
    layers.append(text(MARGIN, top + 58, lede, 34, color=TEXT_2))
    seals = ((110, ACCENT, ON_ACCENT), (92, CLAY_DEEP, TEXT), (80, ACCENT_SOFT, ACCENT_TEXT))
    centres = (top + 220, top + 460, top + 680)
    described: list[str] = []
    for (name, messages), (radius, fill, ink), cy in zip(people, seals, centres, strict=False):
        cx = MARGIN + 110
        mark = initials(name)
        share = messages / everything
        layers.append(circle(cx, cy, radius, fill))
        layers.append(
            text(
                cx,
                cy + 2,
                mark,
                round(radius * 0.72),
                weight=600,
                color=ink,
                align="center",
                baseline="middle",
            )
        )
        column = MARGIN + 260
        first = name if y.options.names else plural(messages, "message")
        if y.options.names:
            second = f"{plural(messages, 'message')}, {share:.0%} of all of yours"
        else:
            second = f"{share:.0%} of all your messages"
        layers.append(text(column, cy - 6, wrap(first, 54, RIGHT - column, 600, 1), 54, weight=600))
        layers.append(text(column, cy + 44, second, 30, color=TEXT_2))
        described.append(f"{name if y.options.names else mark}, {plural(messages, 'message')}")
    alt = f"Your circle in {y.year}: " + "; ".join(described) + "."
    return title, alt, layers


def _soundtrack(y: _Year) -> Built | None:
    top = y.dig("music.top_artists_by_year", top_n=1)
    monthly = y.dig("music.listening_minutes", granularity="month")
    if top is None or monthly is None:
        return None
    best = top.data.to_pylist()[0]
    artist, artist_hours = str(best["artist"]), float(best["minutes"]) / 60
    tracks = best.get("top_tracks") or []
    title = "Your soundtrack"
    layers, cursor = _title(title)
    name = wrap(artist, 100, CONTENT, 600)
    layers.append(
        text(MARGIN, cursor + 140, name, 100, weight=600, color=ACCENT_TEXT, line_height=104)
    )
    cursor += 140 + 104 * (len(name) - 1)
    layers.append(
        text(
            MARGIN,
            cursor + 56,
            f"Your most-played artist, {duration(artist_hours)}",
            32,
            color=TEXT_2,
        )
    )
    cursor += 56
    described = f"Top artist {artist}, {duration(artist_hours)}"
    if tracks:
        track = tracks[0]
        track_hours = float(track["minutes"]) / 60
        layers.append(
            text(
                MARGIN, cursor + 104, wrap(str(track["track"]), 48, CONTENT, 500, 1), 48, weight=500
            )
        )
        layers.append(
            text(
                MARGIN,
                cursor + 148,
                wrap(
                    f"by {track['artist']}, your top track, {duration(track_hours)}",
                    30,
                    CONTENT,
                    400,
                    1,
                ),
                30,
                color=TEXT_2,
            )
        )
        cursor += 148
        described += f"; top track {track['track']} by {track['artist']}"
    minutes = [0.0] * 12
    for row in monthly.data.to_pylist():
        minutes[row["bucket"].month - 1] += float(row["minutes"])
    peak = max(range(12), key=lambda m: (minutes[m], -m))
    low, high = cursor + 70, GROUND - 120
    centre, half = (low + high) / 2, max(24.0, (high - low) / 2)
    slot = CONTENT / 12
    bar = slot * 0.56
    bars = []
    for month, value in enumerate(minutes):
        h = max(bar / 2, half * value / (minutes[peak] or 1))
        x = MARGIN + month * slot + (slot - bar) / 2
        bars.append(
            {
                "x": round(x, 2),
                "x2": round(x + bar, 2),
                "y": round(centre - h, 2),
                "y2": round(centre + h, 2),
                "color": ACCENT if month == peak else MUTED_BAR,
            }
        )
    layers.append(rects(bars, radius=bar / 2))
    layers.extend(
        text(
            MARGIN + month * slot + slot / 2,
            high + 44,
            MONTHS[month],
            24,
            weight=600 if month == peak else 400,
            color=ACCENT_TEXT if month == peak else TEXT_3,
            align="center",
        )
        for month in range(12)
    )
    total = sum(minutes) / 60
    peak_name = date(y.year, peak + 1, 1).strftime("%B")
    layers.append(
        text(
            MARGIN,
            high + 92,
            f"{duration(total).capitalize()} in all, most of them in {peak_name}.",
            30,
            color=TEXT_2,
        )
    )
    alt = (
        f"Your soundtrack in {y.year}: {described}; {duration(total)} in all, most in {peak_name}."
    )
    return title, alt, layers


def _rhythm(y: _Year) -> Built | None:
    result = y.dig("messages.activity_heatmap")
    if result is None or result.headline is None:
        return None
    hours, days = [0] * 24, [0] * 7
    for row in result.data.to_pylist():
        hours[int(row["hour"])] += int(row["messages"])
        days[int(row["weekday"]) - 1] += int(row["messages"])
    night = float(result.headline.value)
    busiest_hour = max(range(24), key=lambda h: (hours[h], -h))
    busiest_day = WEEKDAYS[max(range(7), key=lambda d: (days[d], -d))]
    if night >= 15 or busiest_hour >= 23 or busiest_hour < 4:
        title = "Night owl, confirmed"
    elif 5 <= busiest_hour < 10:
        title = "Early riser"
    else:
        title = "Your rhythm"
    layers, top = _title(title)
    layers.append(
        text(
            MARGIN,
            top + 58,
            f"Busiest on {busiest_day}s, and at {clock(busiest_hour)}.",
            34,
            color=TEXT_2,
        )
    )
    cx, cy = WIDTH / 2, (top + 100 + GROUND - 60) / 2 + 10
    outer = min(300.0, (GROUND - 60 - (top + 100)) / 2 - 40)
    inner = 104.0
    gap = 0.012
    arcs = [
        {
            "t0": round(h * 2 * pi / 24 + gap, 5),
            "t1": round((h + 1) * 2 * pi / 24 - gap, 5),
            "r2": inner,
            "r": round(inner + 10 + (outer - inner - 10) * hours[h] / (max(hours) or 1), 2),
            "color": ACCENT if h < 5 else MUTED_BAR,
        }
        for h in range(24)
    ]
    layers.append(
        {
            "data": {"values": arcs},
            "mark": {"type": "arc", "x": cx, "y": cy, "strokeWidth": 0, "cornerRadius": 3},
            "encoding": {
                "theta": {"field": "t0", **PIXEL},
                "theta2": {"field": "t1"},
                "radius": {"field": "r", **PIXEL},
                "radius2": {"field": "r2"},
                "color": {"field": "color", "type": "nominal", "scale": None},
            },
        }
    )
    layers.insert(-1, shape(cx, cy, "circle", outer * 2 + 24, LINE, filled=False, stroke_width=1))
    label_r = outer + 40
    for hour, align, baseline in (
        (0, "center", "bottom"),
        (6, "left", "middle"),
        (12, "center", "top"),
        (18, "right", "middle"),
    ):
        angle = hour * 2 * pi / 24
        layers.append(
            text(
                cx + label_r * sin(angle),
                cy - label_r * cos(angle),
                clock(hour),
                24,
                color=TEXT_3,
                align=align,
                baseline=baseline,
            )
        )
    layers.append(
        text(
            cx,
            cy + 4,
            f"{night:.0f}%",
            68,
            weight=600,
            color=ACCENT_TEXT,
            align="center",
            baseline="middle",
        )
    )
    layers.append(
        text(cx, cy + 54, "midnight\u20135 am", 22, color=TEXT_3, align="center", baseline="middle")
    )
    alt = (
        f"Your rhythm in {y.year}: {night:.0f}% of your messages went out between midnight "
        f"and 5 am; busiest on {busiest_day}s and at {clock(busiest_hour)}."
    )
    return title, alt, layers


Section = tuple[float, Callable[[float], list[Layer]]]


def _stack(sections: list[Section], top: float, bottom: float) -> list[Layer]:
    """Sections from `top`, spaced evenly with a hairline between, within a calm range."""
    used = sum(height for height, _ in sections)
    gap = max(72.0, min(160.0, (bottom - top - used) / max(len(sections) - 1, 1)))
    layers: list[Layer] = []
    cursor = top
    for index, (height, draw) in enumerate(sections):
        if index:
            layers.append(hairline(cursor - gap / 2))
        layers.extend(draw(cursor))
        cursor += height + gap
    return layers


def _money(y: _Year) -> Built | None:
    if not y.rows("transactions"):
        return None
    currency_rows = y.store.query(
        "SELECT currency FROM transactions WHERE timezone(?, ts)::DATE BETWEEN ? AND ? "
        "GROUP BY currency ORDER BY count(*) DESC, currency LIMIT 1",
        [y.options.tz, date(y.year, 1, 1), date(y.year, 12, 31)],
    ).to_pylist()
    currency = str(currency_rows[0]["currency"])
    subscriptions = y.dig("money.subscriptions", currency=currency)
    active = [r for r in subscriptions.data.to_pylist() if r["active"]] if subscriptions else []
    delivery = y.dig("money.delivery_index", currency=currency)
    savings = y.dig("money.income_vs_spend", currency=currency)
    before = _Year(y.store, y.year - 1, y.options).dig("money.income_vs_spend", currency=currency)
    sections: list[Section] = []
    described: list[str] = []

    # Subscriptions: one coin per subscription still running on 31 December.
    noun = "subscription" if len(active) == 1 else "subscriptions"
    headline = f"{len(active)} {noun} still running" if active else "No subscriptions running"
    if y.options.amounts and active:
        detail = f"{amount(sum(float(r['annual_cost']) for r in active), currency)} a year"
    else:
        detail = "on 31 December" if active else "Nothing renewing quietly. Nice."
    coins = min(len(active), 9)
    offset = 124 if active else 0

    def subscriptions_at(top: float) -> list[Layer]:
        layers: list[Layer] = []
        for index in range(coins):
            cx = MARGIN + 42 + index * 74
            layers.append(circle(cx, top + 42, 38, ACCENT))
            layers.append(
                shape(cx, top + 42, "circle", 52, ACCENT_TEXT, filled=False, stroke_width=2)
            )
        if len(active) > coins:
            layers.append(
                text(
                    MARGIN + coins * 74 + 16,
                    top + 54,
                    f"+{len(active) - coins}",
                    34,
                    weight=500,
                    color=ACCENT_TEXT,
                )
            )
        layers.append(text(MARGIN, top + offset + 48, headline, 48, weight=600))
        layers.append(text(MARGIN, top + offset + 94, detail, 30, color=TEXT_2))
        return layers

    sections.append((offset + 94, subscriptions_at))
    described.append(f"{headline} {detail}")

    # Delivery: its share of the year's spending as one bar.
    rows = delivery.data.to_pylist() if delivery else []
    spend = sum(float(r["spend"]) for r in rows)
    food = sum(float(r["delivery_spend"]) for r in rows)
    if spend > 0:
        share = min(max(0.0, food / spend), 1.0)
        big = f"{share:.0%}"
        line = f"{big} of your spending went on food delivery"
        label = wrap(
            "of your spending went on food delivery", 34, CONTENT - measure(big, 120, 600) - 32
        )

        def delivery_at(top: float) -> list[Layer]:
            label_x = MARGIN + measure(big, 120, 600) + 32
            layers = [
                text(MARGIN - 4, top + 104, big, 120, weight=600, color=ACCENT_TEXT),
                text(
                    label_x,
                    top + 104 - 42 * (len(label) - 1),
                    label,
                    34,
                    color=TEXT_2,
                    line_height=42,
                ),
                rect(MARGIN, top + 140, RIGHT, top + 168, SURFACE_3, radius=14),
            ]
            if share > 0:
                end = MARGIN + max(28.0, CONTENT * share)
                layers.append(rect(MARGIN, top + 140, end, top + 168, ACCENT, radius=14))
            if y.options.amounts:
                layers.append(
                    text(MARGIN, top + 214, f"{amount(food, currency)} in all", 30, color=TEXT_2)
                )
            return layers

        sections.append((214 if y.options.amounts else 168, delivery_at))
        described.append(line)

    # Savings: the direction of the average savings rate against the year before, or
    # within the year when there is no year before.
    rate, previous = _rate(savings), _rate(before)
    change = rate - previous if rate is not None and previous is not None else None
    if change is not None:
        against = f"than in {y.year - 1}"
        subject = "You saved"
    else:
        change, against, subject = _half_change(savings), "than in the first", "In the second half"
        subject += " you saved"
    if change is not None:
        if change > 1:
            path, angle, colour, words = ARROW_UP, 0, DOWN, f"{subject} more {against}"
        elif change < -1:
            path, angle, colour, words = ARROW_UP, 180, UP, f"{subject} less {against}"
        else:
            path, angle, colour = ARROW_FLAT, 0, MUTED_BAR
            words = f"{subject} about as much {against.replace('than', 'as')}"
        if previous is None:
            words += " half"
        lines = wrap(words, 44, CONTENT - 112, 500)
        kept = None
        if y.options.amounts and rate is not None:
            kept = f"You kept {rate:.0f}% of your income on average"
            if previous is not None:
                kept += f", against {previous:.0f}%"
        height = 40 + 52 * len(lines) + (44 if kept else 0)

        def savings_at(top: float) -> list[Layer]:
            layers = [
                shape(MARGIN + 36, top + 34, path, 72, colour, angle=angle),
                text(MARGIN + 112, top + 50, lines, 44, weight=500, line_height=52),
            ]
            if kept:
                layers.append(
                    text(MARGIN + 112, top + 50 + 52 * len(lines) - 4, kept, 30, color=TEXT_2)
                )
            return layers

        sections.append((height, savings_at))
        described.append(words)

    title = "Your money habits"
    layers, top = _title(title)
    layers += _stack(sections, top + 72, GROUND - 60)
    alt = f"Your money habits in {y.year}: " + "; ".join(described) + "."
    return title, alt, layers


def _rate(result: DigResult | None) -> float | None:
    if result is None or result.headline is None:
        return None
    value = result.headline.value
    return float(value) if isinstance(value, int | float) else None


def _half_change(result: DigResult | None) -> float | None:
    """Second-half minus first-half average monthly savings rate, in points."""
    if result is None:
        return None
    halves: tuple[list[float], list[float]] = ([], [])
    for row in result.data.to_pylist():
        if row["savings_rate"] is not None:
            halves[row["bucket"].month > 6].append(float(row["savings_rate"]) * 100)
    if not halves[0] or not halves[1]:
        return None
    return sum(halves[1]) / len(halves[1]) - sum(halves[0]) / len(halves[0])


def _change(y: _Year) -> Built | None:
    dig = registry.discover().get("trends.yoy")
    if dig is None or not _covers(y, date(y.year - 1, 1, 31)):
        return None
    result = dig.compute(y.store, DigParams(date_to=date(y.year, 12, 31), tz=y.options.tz))
    rows = [
        r for r in result.data.to_pylist() if r["delta_pct"] is not None and r["year"] == y.year
    ]
    if not rows:
        return None
    first, others = rows[0], [r for r in rows[1:] if r["delta_pct"]][:OTHER_CHANGES]
    title = "What changed most"
    layers, cursor = _title(title)
    layers.append(
        text(MARGIN, cursor + 58, f"{y.year} compared with {y.year - 1}", 34, color=TEXT_2)
    )
    big = signed(first["delta_pct"], first["delta_unit"])
    layers.append(text(MARGIN - 6, cursor + 268, big, 190, weight=600, color=ACCENT_TEXT))
    name, about = _change_name(first)
    layers.append(text(MARGIN, cursor + 346, wrap(name, 52, CONTENT, 600, 1), 52, weight=600))
    about_lines = wrap(about, 30, CONTENT, 400)
    layers.append(text(MARGIN, cursor + 394, about_lines, 30, color=TEXT_2, line_height=38))
    cursor += 394 + 38 * (len(about_lines) - 1)
    span = _span(first, y.options.amounts)
    if span:
        layers.append(text(MARGIN, cursor + 44, span, 28, color=TEXT_3))
        cursor += 44
    described = f"{name} {big}"
    if others:
        cursor += 60
        layers.append(hairline(cursor))
        widest = max(abs(r["delta_pct"]) for r in others)
        room = (GROUND - 30 - cursor) / len(others)
        step = min(96.0, room)
        for index, row in enumerate(others):
            top = cursor + 24 + index * step
            label, _ = _change_name(row)
            value = signed(row["delta_pct"], row["delta_unit"])
            layers.append(
                text(MARGIN, top + 30, wrap(label, 30, CONTENT - 200, 400, 1), 30, color=TEXT_2)
            )
            layers.append(text(RIGHT, top + 30, value, 30, weight=500, align="right"))
            length = max(8.0, CONTENT * abs(row["delta_pct"]) / widest)
            colour = UP if row["delta_pct"] > 0 else DOWN
            layers.append(rect(MARGIN, top + 46, MARGIN + length, top + 54, colour, radius=4))
            described += f"; {label} {value}"
    alt = f"What changed most from {y.year - 1} to {y.year}: {described}."
    return title, alt, layers


def _covers(y: _Year, by: date) -> bool:
    """Whether the data starts by `by`: a year that began mid-way would make a false change."""
    tables = [t for t in fact_tables(y.store) if y.store.table_counts().get(t)]
    if not tables:
        return False
    union = " UNION ALL ".join(f"SELECT min(ts) AS ts FROM {table}" for table in tables)
    first = y.store.query(
        f"SELECT min(timezone(?, ts)::DATE) AS day FROM ({union})", [y.options.tz]
    ).to_pylist()[0]["day"]
    return first is not None and first <= by


def _change_name(row: dict[str, Any]) -> tuple[str, str]:
    dig_id = str(row["dig_id"])
    if dig_id == "money.spend_by_category":
        category = str(row["label"]).replace("_", " ")
        return f"Spending on {category}", "Your biggest category, in both years"
    return CHANGE_NAMES.get(dig_id, (str(row["dig_title"]), ""))


def _span(row: dict[str, Any], amounts: bool) -> str | None:
    """'301 hours in 2024, 287 in 2025'; money values only when amounts are allowed."""
    unit = str(row["unit"] or "")
    money = str(row["dig_id"]).startswith("money.") and unit not in {"%", "percent"}
    if money and not amounts:
        return None
    if str(row["dig_id"]) == "messages.volume_by_contact":
        return None

    def show(value: float) -> str:
        if unit == "%":
            return f"{value:.1f}%"
        if money:
            return amount(value, unit)
        return f"{value:,.1f}".removesuffix(".0") + (f" {unit}" if unit else "")

    return (
        f"{show(float(row['last_year']))} in {row['previous_year']}, "
        f"{show(float(row['this_year']))} in {row['year']}"
    )


SLOTS: tuple[tuple[str, Callable[[_Year], Built | None]], ...] = (
    ("numbers", _numbers),
    ("circle", _circle),
    ("soundtrack", _soundtrack),
    ("rhythm", _rhythm),
    ("money", _money),
    ("change", _change),
)


def build(store: Store, year: int, options: WrappedOptions | None = None) -> list[WrappedCard]:
    """The year's cards in slot order; slots without data are left out."""
    y = _Year(store, year, options or WrappedOptions())
    built = [(key, made) for key, make in SLOTS if (made := make(y)) is not None]
    cards: list[WrappedCard] = []
    for number, (key, (title, alt, layers)) in enumerate(built, start=1):
        seed = [slot for slot, _ in SLOTS].index(key)
        cards.append(
            WrappedCard(key, title, alt, _spec(_frame(seed, year, number, len(built)) + layers))
        )
    return cards


def render(
    store: Store, year: int, options: WrappedOptions | None = None
) -> list[tuple[WrappedCard, bytes]]:
    return [(card, render_png(card)) for card in build(store, year, options)]


__all__: Sequence[str] = (
    "WrappedCard",
    "WrappedError",
    "WrappedOptions",
    "build",
    "default_year",
    "render",
    "render_png",
    "years",
)
