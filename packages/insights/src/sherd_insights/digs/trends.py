"""Year-over-year comparisons of available deterministic numeric headlines."""

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from math import isfinite
from typing import Any

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline
from sherd_insights.digs.cross_metrics import fact_tables
from sherd_insights.digs.money_metrics import _selection as money_selection
from sherd_insights.registry import available

logger = logging.getLogger(__name__)

_FIELDS: dict[str, pa.DataType] = {
    "dig_id": pa.string(),
    "label": pa.string(),
    "dig_title": pa.string(),
    "delta_unit": pa.string(),
    "skipped_digs": pa.int64(),
    "unit": pa.string(),
    "this_year": pa.float64(),
    "last_year": pa.float64(),
    "delta_pct": pa.float64(),
    "year": pa.int64(),
    "previous_year": pa.int64(),
}
SCHEMA = pa.schema(_FIELDS)


def _empty(skipped: int = 0) -> DigResult:
    return DigResult(
        pa.Table.from_pylist([], schema=SCHEMA).replace_schema_metadata(
            {"skipped_digs": str(skipped)}
        ),
        None,
        f"No comparable yearly headlines yet. Skipped {skipped} digs.",
        None,
        "No chart: two years of comparable numeric headlines are needed.",
    )


@dataclass
class YearOverYear:
    id: str = "trends.yoy"
    title: str = "Year-over-year changes"
    requires: list[str] = field(default_factory=list)

    def compute(self, store: Store, params: DigParams) -> DigResult:
        tables = fact_tables(store)
        if not tables:
            return _empty()
        # Only aggregate timestamps cross the Python boundary.
        union = " UNION ALL ".join(f"SELECT max(ts) AS ts FROM {table}" for table in tables)
        latest = store.query(
            "SELECT max(timezone(?, ts)::DATE) AS day FROM (" + union + ")", [params.tz]
        ).to_pylist()[0]["day"]
        if latest is None:
            return _empty()
        cutoff = min(latest, params.date_to) if params.date_to else latest
        year = cutoff.year if (cutoff.month, cutoff.day) == (12, 31) else cutoff.year - 1
        current = replace(params, date_from=date(year, 1, 1), date_to=date(year, 12, 31))
        previous = replace(params, date_from=date(year - 1, 1, 1), date_to=date(year - 1, 12, 31))
        # Keep a single currency across years; counts in the combined range break ties by code.
        if params.currency is None and "transactions" in tables:
            _, _, currency = money_selection(store, replace(current, date_from=previous.date_from))
            current, previous = (
                replace(current, currency=currency),
                replace(previous, currency=currency),
            )
        rows: list[dict[str, Any]] = []
        skipped = 0
        for dig in available(store):
            if dig.id == self.id:
                continue
            skipped += 1
            if dig.id in {
                "cross.life_timeline",
                "cross.spend_vs_listening",
                "cross.messages_vs_youtube",
            }:
                continue
            try:
                this, last = dig.compute(store, current), dig.compute(store, previous)
                if not this.data.num_rows or not last.data.num_rows:
                    continue
                if this.headline is None or last.headline is None:
                    continue
                if this.headline.unit == "r" or this.headline.label != last.headline.label:
                    continue
                a, b = this.headline.value, last.headline.value
                if isinstance(a, bool) or isinstance(b, bool):
                    continue
                if not isinstance(a, int | float) or not isinstance(b, int | float):
                    continue
                if not isfinite(a) or not isfinite(b) or this.headline.unit != last.headline.unit:
                    continue
                a, b = round(float(a), 10), round(float(b), 10)
                delta_unit = "pp" if this.headline.unit in {"%", "percent", "percentage"} else "%"
                delta = (
                    round(a - b, 10)
                    if delta_unit == "pp"
                    else round((a - b) / abs(b) * 100, 10)
                    if b
                    else None
                )
                if delta is not None and not isfinite(delta):
                    delta = None
                rows.append(
                    {
                        "dig_id": dig.id,
                        "label": this.headline.label,
                        "dig_title": dig.title,
                        "delta_unit": delta_unit,
                        "unit": this.headline.unit,
                        "this_year": a,
                        "last_year": b,
                        "delta_pct": delta,
                        "year": year,
                        "previous_year": year - 1,
                    }
                )
                skipped -= 1
            except Exception as exc:
                # Exception text and tracebacks may contain private content.
                logger.warning("Skipped dig %s (%s)", dig.id, type(exc).__name__)
        if not rows:
            return _empty(skipped)
        for row in rows:
            row["skipped_digs"] = skipped
        rows.sort(
            key=lambda r: (
                -(abs(r["delta_pct"]) if r["delta_pct"] is not None else -1),
                r["dig_id"],
            )
        )
        data = pa.Table.from_pylist(rows, schema=SCHEMA).replace_schema_metadata(
            {"skipped_digs": str(skipped)}
        )
        comparable = [r for r in rows if r["delta_pct"] is not None]
        if not comparable:
            return DigResult(
                data,
                None,
                f"Yearly headlines for {year} and {year - 1} have "
                f"zero baselines; percentage changes are undefined. Skipped {skipped} digs.",
                None,
                "No percentage chart: all previous-year headline values are zero.",
            )
        chart = charts.bar(comparable, "label", "delta_pct")
        chart["encoding"]["y"]["title"] = None
        chart["encoding"]["x"]["title"] = f"Change from {year - 1} to {year} (% or pp)"
        chart["encoding"]["color"] = {
            "condition": {
                "test": "datum.delta_pct >= 0",
                "value": charts.load_theme()["range"]["category"][0],
            },
            "value": charts.load_theme()["range"]["category"][1],
        }
        chart["encoding"]["tooltip"] = [
            {"field": "label"},
            {"field": "dig_title"},
            {"field": "unit"},
            {"field": "delta_unit"},
            {"field": "this_year"},
            {"field": "last_year"},
            {"field": "delta_pct", "format": ".1f"},
        ]
        # Explicit zero rule separates decreases and increases, including one-sided results.
        chart["layer"] = [
            {"mark": chart.pop("mark")},
            {
                "mark": {"type": "rule", "color": "#666666"},
                "encoding": {
                    "x": {"datum": 0},
                    "y": {"value": 0},
                    "y2": {"value": chart["height"]},
                    "color": {"value": "#666666"},
                },
            },
        ]
        winner = comparable[0]
        return DigResult(
            data,
            chart,
            f"The biggest absolute change from {year - 1} to {year} was "
            f"{winner['label']}: {winner['delta_pct']:+.1f} {winner['delta_unit']}. "
            f"Skipped {skipped} digs.",
            Headline(str(winner["label"]), winner["delta_pct"], winner["delta_unit"]),
            f"Headline changes from {year - 1} to {year}. "
            "Each bar compares the same dig and unit in both full local calendar years; "
            "Percentage headlines use percentage points (pp); quantities use relative %. "
            "Zero quantity baselines have no relative change. "
            + "; ".join(
                f"{r['label']}: {r['delta_pct']:+.1f} {r['delta_unit']}" for r in comparable
            ),
        )


DIGS: list[Dig] = [YearOverYear()]
