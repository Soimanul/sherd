"""Messages, Music and Money: headline tiles, then one chart card per dig of the domain."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sherd_insights import registry

from sherd_web.app import compute
from sherd_web.deps import SettingsDep, StoreDep, local_zone, render
from sherd_web.routes._pages import (
    GRANULARITIES,
    TOP_N,
    currencies,
    data_span,
    in_order,
    is_empty,
    parse_filters,
    partial,
    place,
    shell,
    with_prefix,
)

router = APIRouter()

RESULTS_ID = "dig-results"


@dataclass(frozen=True)
class Domain:
    path: str
    title: str
    prefix: str
    table: str
    lede: str
    # Reading order: the overview first, then the detail. Digs not named here follow by id.
    order: tuple[str, ...]
    wide: frozenset[str]
    has_currency: bool = False


DOMAINS = (
    Domain(
        "/messages",
        "Messages",
        "messages.",
        "messages",
        "pages.messages.lede",
        (
            "messages.volume_by_contact",
            "messages.top_contacts_by_year",
            "messages.activity_heatmap",
            "messages.response_times",
            "messages.conversation_starters",
            "messages.streaks_silences",
            "messages.emoji_words",
        ),
        frozenset(
            {
                "messages.volume_by_contact",
                "messages.top_contacts_by_year",
                "messages.activity_heatmap",
            }
        ),
    ),
    Domain(
        "/music",
        "Music",
        "music.",
        "media_plays",
        "pages.music.lede",
        (
            "music.listening_minutes",
            "music.top_artists_by_year",
            "music.discovery_rate",
            "music.skips_obsessions",
            "music.seasonality",
        ),
        frozenset({"music.listening_minutes", "music.top_artists_by_year", "music.seasonality"}),
    ),
    Domain(
        "/money",
        "Money",
        "money.",
        "transactions",
        "pages.money.lede",
        (
            "money.spend_by_category",
            "money.income_vs_spend",
            "money.delivery_index",
            "money.subscriptions",
            "money.weekday_weekend",
        ),
        frozenset({"money.spend_by_category", "money.subscriptions", "money.weekday_weekend"}),
        has_currency=True,
    ),
)


def _page(domain: Domain) -> Callable[..., Response]:
    def page(request: Request, store: StoreDep, settings: SettingsDep) -> Response:
        name = domain.path.strip("/")
        context = shell(
            store,
            domain=domain,
            name=name,
            results_id=RESULTS_ID,
            top_n_choices=TOP_N,
            granularities=GRANULARITIES,
        )
        rows = context["counts"].get(domain.table, 0)
        if store is None or not rows:
            return render(request, "pages/domain.html", {**context, "empty": True})

        tz = local_zone()
        options = currencies(store) if domain.has_currency else []
        filters = parse_filters(request.query_params, options)
        digs = in_order(with_prefix(registry.available(store), domain.prefix), domain.order)
        cards, failed = compute(store, digs, filters.params(tz))
        installed = with_prefix(registry.discover().values(), domain.prefix)
        waiting = [dig for dig in installed if dig not in digs]
        context.update(
            empty=False,
            rows=rows,
            span=data_span(store, domain.table, tz),
            filters=filters,
            currencies=options,
            tz=tz,
            tiles=[c for c in cards if c.result.headline is not None and not is_empty(c)],
            placed=place(cards, domain.wide),
            failed=failed,
            waiting=waiting,
            nothing_in_range=bool(cards) and all(is_empty(c) for c in cards),
        )
        if partial(request) == RESULTS_ID:
            return render(request, "pages/_domain_results.html", {**context, "oob": True})
        return render(request, "pages/domain.html", context)

    page.__name__ = domain.path.strip("/")
    return page


for _domain in DOMAINS:
    router.add_api_route(_domain.path, _page(_domain), methods=["GET"], response_class=HTMLResponse)
