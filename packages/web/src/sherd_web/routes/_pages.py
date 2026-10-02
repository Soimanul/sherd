"""What the WP-15 pages share: filters, digs found by id prefix, and the POST guard.

No `router` here, so `sherd_web.app` does not mount this module.
"""

import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import pyarrow as pa
import pyarrow.types as pat
from fastapi import Depends, HTTPException, Request
from sherd_core import Store
from sherd_insights import Dig, DigParams, registry

from sherd_web.app import Card, compute

Granularity = Literal["day", "week", "month", "year"]
GRANULARITIES: tuple[Granularity, ...] = ("day", "week", "month", "year")
TOP_N = (5, 10, 20, 50)
DEFAULT_TOP_N = DigParams.top_n
DEFAULT_GRANULARITY: Granularity = DigParams.granularity
FORM_LIMIT = 64 * 1024

# One token per server process: forms carry it, and a cross-site page cannot read it.
CSRF_TOKEN = secrets.token_urlsafe(32)


# ---- Filters ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """The filter bar's values; anything left out means "the whole range" or the default."""

    date_from: date | None = None
    date_to: date | None = None
    top_n: int = DEFAULT_TOP_N
    granularity: Granularity = DEFAULT_GRANULARITY
    currency: str | None = None
    problems: tuple[str, ...] = ()

    def params(self, tz: str) -> DigParams:
        return DigParams(
            date_from=self.date_from,
            date_to=self.date_to,
            top_n=self.top_n,
            granularity=self.granularity,
            tz=tz,
            currency=self.currency,
        )

    @property
    def is_default(self) -> bool:
        return self == Filters(problems=self.problems)

    def query(self) -> str:
        """The non-default values as a query string, for links that keep the filters."""
        values: dict[str, str] = {}
        if self.date_from:
            values["date_from"] = self.date_from.isoformat()
        if self.date_to:
            values["date_to"] = self.date_to.isoformat()
        if self.top_n != DEFAULT_TOP_N:
            values["top_n"] = str(self.top_n)
        if self.granularity != DEFAULT_GRANULARITY:
            values["granularity"] = self.granularity
        if self.currency:
            values["currency"] = self.currency
        return urlencode(values)


def _date(value: str, label: str, problems: list[str]) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        problems.append(f"{label} is not a date in YYYY-MM-DD form, so it was left out.")
        return None


def parse_filters(query: Mapping[str, str], currencies: Sequence[str] = ()) -> Filters:
    """Read the filter bar from a query string. Bad values fall back to the default and say so."""
    problems: list[str] = []
    date_from = _date(query.get("date_from", "").strip(), "From", problems)
    date_to = _date(query.get("date_to", "").strip(), "To", problems)
    if date_from and date_to and date_from > date_to:
        problems.append("From is after To, so the whole range is shown.")
        date_from = date_to = None

    top_n = DEFAULT_TOP_N
    raw_top = query.get("top_n", "").strip()
    if raw_top:
        if raw_top.isdigit() and int(raw_top) in TOP_N:
            top_n = int(raw_top)
        else:
            problems.append(f"Top can be {', '.join(map(str, TOP_N))}; showing {DEFAULT_TOP_N}.")

    granularity = DEFAULT_GRANULARITY
    raw_granularity = query.get("granularity", "").strip()
    if raw_granularity:
        if raw_granularity in GRANULARITIES:
            granularity = raw_granularity
        else:
            problems.append(f"Unknown grouping, so it is by {DEFAULT_GRANULARITY}.")

    currency = None
    raw_currency = query.get("currency", "").strip().upper()
    if raw_currency:
        if raw_currency in currencies:
            currency = raw_currency
        else:
            problems.append("There are no transactions in that currency; showing the main one.")
    return Filters(date_from, date_to, top_n, granularity, currency, tuple(problems))


def currencies(store: Store) -> list[str]:
    """Currencies in the ledger, most transactions first (the digs' default comes first)."""
    rows = store.query(
        "SELECT currency, count(*) AS n FROM transactions GROUP BY currency"
        " ORDER BY n DESC, currency"
    ).to_pylist()
    return [str(row["currency"]) for row in rows]


def data_span(store: Store, table: str, tz: str) -> tuple[date, date] | None:
    """First and last local day with rows in a fact table."""
    if table not in store.table_counts():
        return None
    row = store.query(
        f"SELECT min(timezone(?, ts))::DATE AS lo, max(timezone(?, ts))::DATE AS hi FROM {table}",
        [tz, tz],
    ).to_pylist()[0]
    if row["lo"] is None:
        return None
    return row["lo"], row["hi"]


# ---- Digs -------------------------------------------------------------------------------------


def with_prefix(digs: Iterable[Dig], prefix: str) -> list[Dig]:
    return [dig for dig in digs if dig.id.startswith(prefix)]


def in_order(digs: Iterable[Dig], preferred: Sequence[str]) -> list[Dig]:
    """`preferred` ids first, in that order; digs it does not name follow by id."""
    rank = {dig_id: index for index, dig_id in enumerate(preferred)}
    return sorted(digs, key=lambda dig: (rank.get(dig.id, len(rank)), dig.id))


@dataclass(frozen=True)
class Placed:
    """A chart card and whether it spans the whole grid row."""

    card: Card
    wide: bool


def place(cards: Sequence[Card], wide: Iterable[str]) -> list[Placed]:
    """Mark wide cards; a half-width card left without a partner widens to fill its row."""
    wide_ids = set(wide)
    placed: list[Placed] = []
    open_half: int | None = None
    for card in cards:
        if card.dig_id in wide_ids:
            if open_half is not None:
                placed[open_half] = replace(placed[open_half], wide=True)
                open_half = None
            placed.append(Placed(card, True))
        else:
            placed.append(Placed(card, False))
            open_half = len(placed) - 1 if open_half is None else None
    if open_half is not None:
        placed[open_half] = replace(placed[open_half], wide=True)
    return placed


@dataclass(frozen=True)
class Slot:
    """One expected dig on a page: its card, or why there is none."""

    dig_id: str
    card: Card | None = None
    missing: bool = False  # not installed
    unavailable: Dig | None = None  # installed, but its tables have no rows
    failed: Dig | None = None


def slot(store: Store, dig_id: str, params: DigParams) -> Slot:
    """Compute one dig by id; missing, unavailable and failing digs become empty states."""
    dig = registry.discover().get(dig_id)
    if dig is None:
        return Slot(dig_id, missing=True)
    if dig not in registry.available(store):
        return Slot(dig_id, unavailable=dig)
    cards, failed = compute(store, [dig], params)
    if failed:
        return Slot(dig_id, failed=dig)
    return Slot(dig_id, card=cards[0])


def numeric(arrow_type: pa.DataType) -> bool:
    """Right-align these columns in tables."""
    return bool(
        pat.is_integer(arrow_type) or pat.is_floating(arrow_type) or pat.is_decimal(arrow_type)
    )


def is_empty(card: Card) -> bool:
    return card.result.data.num_rows == 0


def table_names(digs: Iterable[Dig]) -> str:
    tables = sorted({table for dig in digs for table in dig.requires})
    return ", ".join(tables)


# ---- HTMX -------------------------------------------------------------------------------------


def partial(request: Request) -> str | None:
    """The id of the element HTMX asked to replace, or None for a full page.

    History restores ask for the whole page even though HTMX sends them.
    """
    if request.headers.get("hx-request") != "true":
        return None
    if request.headers.get("hx-history-restore-request") == "true":
        return None
    return request.headers.get("hx-target") or ""


# ---- POST guard -------------------------------------------------------------------------------


def _same_origin(request: Request) -> bool:
    """True only with positive evidence that the form came from this server's own pages.

    The shell sets `Referrer-Policy: no-referrer`, so browsers send `Origin: null` and no
    Referer on its POSTs; `Sec-Fetch-Site` still says whether the request is same-origin.
    """
    host = request.headers.get("host", "")
    own = f"{request.url.scheme}://{host}"
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin == own
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}" == own
    return request.headers.get("sec-fetch-site") == "same-origin"


async def posted_form(request: Request) -> dict[str, str]:
    """The URL-encoded form of a same-origin POST that carries this process's CSRF token.

    Anything else is refused with 403 before the route runs.
    """
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin form")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_LIMIT:
            raise HTTPException(status_code=413, detail="form too large")
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="form is not UTF-8") from None
    form = dict(pairs)
    if not secrets.compare_digest(form.get("csrf", "").encode(), CSRF_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="missing or invalid form token")
    return form


Form = Annotated[dict[str, str], Depends(posted_form)]


PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "ollama": "Ollama",
    "stub": "the stub provider",
}


def provider_label(name: str) -> str:
    return PROVIDER_NAMES.get(name, name)


def shell(store: Store | None, **context: Any) -> dict[str, Any]:
    """Page context with the nav counts every shell page shows."""
    counts = store.table_counts() if store is not None else {}
    return {"counts": counts, "csrf": CSRF_TOKEN, "provider_label": provider_label, **context}
