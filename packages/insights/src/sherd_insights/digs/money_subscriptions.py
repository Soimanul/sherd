"""Recurring charge detection, including consecutive stable price changes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline
from sherd_insights.digs.money_metrics import _currency_note, _empty, _selection

_PRICE = pa.struct(
    [("first_charge", pa.date32()), ("last_charge", pa.date32()), ("amount", pa.decimal128(18, 2))]
)
_FIELDS: list[pa.Field[Any]] = [
    pa.field("merchant", pa.string()),
    pa.field("period", pa.string()),
    pa.field("typical_amount", pa.decimal128(18, 2)),
    pa.field("annual_cost", pa.decimal128(20, 2)),
    pa.field("first_charge", pa.date32()),
    pa.field("last_charge", pa.date32()),
    pa.field("active", pa.bool_()),
    pa.field("next_expected_date", pa.date32()),
    pa.field("interval_days", pa.float64()),
    pa.field("charges", pa.int64()),
    pa.field("price_history", pa.list_(_PRICE)),
]
_SCHEMA = pa.schema(_FIELDS)


def _charges(
    store: Store, prefix: str, args: list[object], merchant: str
) -> Iterator[tuple[date, Decimal]]:
    """Bounded reads of one qualifying merchant, never the full transaction history."""
    previous_ts: datetime | None = None
    previous_id = ""
    while True:
        rows = store.query(
            prefix
            + """SELECT local_ts::DATE AS day, -amount AS amount, ts, id
            FROM selected WHERE merchant_label = ? AND amount < 0
            AND kind NOT IN ('exchange','topup')
            AND (?::TIMESTAMPTZ IS NULL OR (ts, id) > (?::TIMESTAMPTZ, ?))
            ORDER BY ts, id LIMIT 1024""",
            [*args, merchant, previous_ts, previous_ts, previous_id],
        ).to_pylist()
        if not rows:
            return
        for row in rows:
            yield row["day"], row["amount"]
        previous_ts, previous_id = rows[-1]["ts"], rows[-1]["id"]
        if len(rows) < 1024:
            return


def _price_runs(charges: list[tuple[date, Decimal]]) -> list[list[tuple[date, Decimal]]]:
    runs: list[list[tuple[date, Decimal]]] = []
    current: list[tuple[date, Decimal]] = []
    for charge in charges:
        candidate = [*current, charge]
        typical = median(amount for _, amount in candidate)
        if current and any(
            abs(amount - typical) > typical * Decimal("0.1") for _, amount in candidate
        ):
            runs.append(current)
            current = [charge]
        else:
            current = candidate
    if current:
        runs.append(current)
    return runs


@dataclass
class SubscriptionDig:
    id: str = "money.subscriptions"
    title: str = "Recurring subscriptions"
    requires: list[str] = field(default_factory=lambda: ["transactions"])

    def compute(self, store: Store, params: DigParams) -> DigResult:
        prefix, args, currency = _selection(store, params)
        # Median cadence is calculated over consecutive local dates, not UTC elapsed hours.
        candidates = store.query(
            prefix
            + """, ordered AS (
            SELECT merchant_label AS merchant, local_ts::DATE AS day,
                lag(local_ts::DATE) OVER (PARTITION BY merchant_label ORDER BY ts, id) AS previous
            FROM selected WHERE amount < 0 AND kind NOT IN ('exchange','topup')
                AND merchant_label IS NOT NULL
        ), cadence AS (SELECT merchant, count(*) AS charges,
            median(date_diff('day', previous, day)) AS interval_days
            FROM ordered GROUP BY merchant HAVING count(*) >= 3)
        SELECT * FROM cadence WHERE interval_days BETWEEN 25 AND 35
            OR interval_days BETWEEN 350 AND 380 ORDER BY merchant""",
            args,
        ).to_pylist()
        reference = params.date_to or datetime.now(ZoneInfo(params.tz)).date()
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            charges = list(_charges(store, prefix, args, candidate["merchant"]))
            runs = _price_runs(charges)
            # Coordinator's price-change contract: established runs need >=3 charges;
            # the latest may have just one, so a recent price rise is visible immediately.
            if any(len(run) < 3 for run in runs[:-1]):
                continue
            typical = median(amount for _, amount in runs[-1]).quantize(Decimal("0.01"))
            interval = float(candidate["interval_days"])
            period = "monthly" if interval <= 35 else "yearly"
            rows.append(
                {
                    "merchant": candidate["merchant"],
                    "period": period,
                    "typical_amount": typical,
                    "annual_cost": typical * (12 if period == "monthly" else 1),
                    "first_charge": charges[0][0],
                    "last_charge": charges[-1][0],
                    "active": (reference - charges[-1][0]).days <= interval * 1.5,
                    "next_expected_date": charges[-1][0] + timedelta(days=round(interval)),
                    "interval_days": interval,
                    "charges": len(charges),
                    "price_history": [
                        {
                            "first_charge": run[0][0],
                            "last_charge": run[-1][0],
                            "amount": median(amount for _, amount in run).quantize(Decimal("0.01")),
                        }
                        for run in runs
                    ],
                }
            )
        rows.sort(key=lambda row: (-row["annual_cost"], row["merchant"]))
        data = pa.Table.from_pylist(rows, schema=_SCHEMA)
        if not rows:
            return _empty(data)
        annual = sum((r["annual_cost"] for r in rows if r["active"]), Decimal("0"))
        chart = charts.bar(
            data, "merchant", "annual_cost", series="status", series_order=["active", "inactive"]
        )
        chart["data"]["values"] = charts.records(
            [{**row, "status": "active" if row["active"] else "inactive"} for row in rows]
        )
        chart["encoding"]["x"]["title"] = f"Annual cost ({currency})"
        chart["encoding"]["y"]["title"] = None
        changed = sum(len(r["price_history"]) > 1 for r in rows)
        note = _currency_note(currency, params) if currency else ""
        narrative = f"Your active subscriptions add up to {annual:.2f} a year."
        if changed:
            narrative += f" {changed} recurring merchant(s) changed price."
        return DigResult(
            data,
            chart,
            narrative + note,
            Headline("Active annual subscription cost", float(annual), currency),
            "Annual cost at the latest stable price, with active and inactive subscriptions. "
            "At least three charges and a median cadence of 25-35 or 350-380 days; "
            "amounts within 10% of each price run's median. Activity uses the range end "
            "or today's local date for an open range, allowing 1.5 typical periods." + note,
        )


DIGS: list[Dig] = [SubscriptionDig()]
