"""Wrapped (WP-18): the year's cards, rendered on this machine and served from memory.

The page lists the cards; each card is a PNG route, so `img-src 'self'` covers it and
nothing is written to disk. Decks are cached per database version and options.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sherd_core import Store
from sherd_insights import wrapped as cards
from sherd_insights.wrapped import WrappedCard, WrappedOptions

from sherd_web.deps import SettingsDep, StoreDep, local_zone, render

router = APIRouter()

DECKS_KEPT = 4  # each deck holds at most six PNGs of ~100 kB
YEARS_SHOWN = 12


@dataclass
class Deck:
    cards: list[WrappedCard]
    pngs: dict[int, bytes] = field(default_factory=dict)


_decks: OrderedDict[tuple[object, ...], Deck] = OrderedDict()
_lock = threading.Lock()


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "on", "yes"}


def _options(names: str | None, amounts: str | None) -> WrappedOptions:
    return WrappedOptions(
        names=_flag(names, cards.SHOW_NAMES),
        amounts=_flag(amounts, cards.SHOW_AMOUNTS),
        tz=local_zone(),
    )


def deck(store: Store, db_path: Path, year: int, options: WrappedOptions) -> Deck:
    """The cards for `year`, cached until the database file changes."""
    stat = db_path.stat()
    key = (str(db_path), stat.st_mtime_ns, stat.st_size, year, options)
    with _lock:
        if key in _decks:
            _decks.move_to_end(key)
            return _decks[key]
    built = Deck(cards.build(store, year, options))
    with _lock:
        _decks[key] = built
        while len(_decks) > DECKS_KEPT:
            _decks.popitem(last=False)
    return built


def _year(value: str) -> int | None:
    """Bound conversion before accepting an ASCII four-digit calendar year."""
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 1000 <= parsed <= 9999 else None


def _query(year: int, options: WrappedOptions) -> str:
    return f"year={year}&names={int(options.names)}&amounts={int(options.amounts)}"


@router.get("/wrapped", response_class=HTMLResponse)
def wrapped_page(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    year: Annotated[str | None, Query()] = None,
    names: Annotated[str | None, Query()] = None,
    amounts: Annotated[str | None, Query()] = None,
) -> Response:
    options = _options(names, amounts)
    context: dict[str, object] = {"options": options, "fallback_year": date.today().year - 1}
    if store is None:
        return render(request, "pages/wrapped.html", {**context, "cards": []})
    counts = store.table_counts()
    available = [y for y in cards.years(store, options.tz) if 1000 <= y <= 9999]
    default = cards.default_year(store, options.tz)
    chosen = default
    problem = None
    if year is not None:
        parsed = _year(year)
        if parsed in available:
            chosen = parsed
        else:
            problem = "There is nothing to wrap for that year, so the default year is shown."
    if chosen is None and available:
        chosen = available[0]
    built = deck(store, settings.db_path, chosen, options).cards if chosen else []
    return render(
        request,
        "pages/wrapped.html",
        {
            **context,
            "counts": counts,
            "cards": built,
            "year": chosen,
            "years": available[:YEARS_SHOWN],
            "default_year": default,
            "problem": problem,
            "query": _query(chosen, options) if chosen else "",
        },
    )


@router.get("/wrapped/{year}/{number}.png")
def wrapped_png(
    store: StoreDep,
    settings: SettingsDep,
    year: str,
    number: int,
    names: Annotated[str | None, Query()] = None,
    amounts: Annotated[str | None, Query()] = None,
) -> Response:
    parsed = _year(year)
    if store is None or parsed is None:
        raise HTTPException(status_code=404)
    options = _options(names, amounts)
    if parsed not in cards.years(store, options.tz):
        raise HTTPException(status_code=404)
    found = deck(store, settings.db_path, parsed, options)
    if not 1 <= number <= len(found.cards):
        raise HTTPException(status_code=404)
    with _lock:
        png = found.pngs.get(number)
    if png is None:
        png = cards.render_png(found.cards[number - 1])
        with _lock:
            found.pngs[number] = png
    return Response(
        png,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="wrapped-{year}-{number}.png"'},
    )
