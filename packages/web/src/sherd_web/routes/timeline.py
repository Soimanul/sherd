"""Timeline: the life timeline, a period's soundtrack, year-over-year trends and correlations.

The cross-source and trends digs are found by id at request time; one that is not installed
or has no data shows an empty state in its place.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sherd_insights import DigParams, registry
from sherd_insights.charts import records

from sherd_web.app import Card, compute
from sherd_web.deps import SettingsDep, StoreDep, local_zone, render
from sherd_web.routes._pages import (
    GRANULARITIES,
    data_span,
    in_order,
    parse_filters,
    partial,
    shell,
    slot,
    with_prefix,
)

router = APIRouter()

CONTROLS_ID = "timeline-controls"
LIFE = "cross.life_timeline"
SOUNDTRACK = "cross.soundtrack"
TRENDS = "trends.yoy"
CORRELATIONS = ("cross.spend_vs_listening", "cross.messages_vs_youtube")
PLACED = {LIFE, SOUNDTRACK, TRENDS, *CORRELATIONS}
# The columns of trends.yoy that make the table of headline changes.
CHANGE_COLUMNS = ("label", "unit", "last_year", "this_year", "delta_pct")


def headline_changes(card: Card | None) -> list[dict[str, Any]]:
    """Rows of the trends table, or [] when the dig's data has another shape."""
    if card is None:
        return []
    names = card.result.data.column_names
    if not set(CHANGE_COLUMNS) <= set(names):
        return []
    extra = [n for n in ("delta_unit", "year") if n in names]
    return records(card.result.data.select([*CHANGE_COLUMNS, *extra]))


@router.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, store: StoreDep, settings: SettingsDep) -> Response:
    context = shell(store, controls_id=CONTROLS_ID, granularities=GRANULARITIES)
    if store is None or not any(context["counts"].values()):
        return render(request, "pages/timeline.html", {**context, "empty": True})

    tz = local_zone()
    query = request.query_params
    life_filters = parse_filters({"granularity": query.get("granularity", "")})
    range_filters = parse_filters(
        {"date_from": query.get("date_from", ""), "date_to": query.get("date_to", "")}
    )
    life = slot(store, LIFE, life_filters.params(tz))
    soundtrack = slot(store, SOUNDTRACK, range_filters.params(tz))
    context.update(
        empty=False,
        tz=tz,
        granularity=life_filters.granularity,
        range=range_filters,
        problems=life_filters.problems + range_filters.problems,
        span=data_span(store, "media_plays", tz) or data_span(store, "messages", tz),
        life=life,
        soundtrack=soundtrack,
    )
    if partial(request) == CONTROLS_ID:
        return render(request, "pages/_timeline_controls.html", context)

    params = DigParams(tz=tz)
    trends = slot(store, TRENDS, params)
    extra = [
        dig
        for dig in in_order(
            with_prefix(registry.available(store), "cross.")
            + with_prefix(registry.available(store), "trends."),
            (),
        )
        if dig.id not in PLACED
    ]
    extra_cards, extra_failed = compute(store, extra, params)
    context.update(
        trends=trends,
        changes=headline_changes(trends.card),
        correlations=[slot(store, dig_id, params) for dig_id in CORRELATIONS],
        extra=extra_cards,
        failed=extra_failed,
    )
    return render(request, "pages/timeline.html", context)
