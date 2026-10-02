"""Single-currency money digs; refunds reduce spend when they occur."""

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from sherd_core import Store

from sherd_insights import charts
from sherd_insights.base import Dig, DigParams, DigResult, Headline

# Negative refunds also remain signed: reversing a refund is spending.
SPEND = """CASE WHEN kind = 'refund' THEN -amount
    WHEN amount < 0 AND kind NOT IN ('exchange','topup') THEN -amount ELSE 0 END"""
INCOME = """CASE WHEN amount > 0 AND kind NOT IN ('exchange','topup','refund')
    THEN amount ELSE 0 END"""
DELIVERY = (
    r"(?i)\b(glovo|tazz|bolt food|uber eats|deliveroo|wolt|just eat|foodpanda|"
    r"doordash|takeaway|lieferando)\b"
)


def _selection(store: Store, params: DigParams) -> tuple[str, list[object], str | None]:
    if params.top_n < 1:
        raise ValueError("top_n must be at least 1")
    prefix = """WITH local AS (
        SELECT *, timezone(?, ts) AS local_ts, coalesce(merchant, merchant_raw) AS merchant_label,
            coalesce(category, 'Uncategorised') AS category_label FROM transactions
    ), in_range AS (
        SELECT * FROM local WHERE (?::DATE IS NULL OR local_ts::DATE >= ?::DATE)
        AND (?::DATE IS NULL OR local_ts::DATE <= ?::DATE)
    ) """
    args: list[object] = [
        params.tz,
        params.date_from,
        params.date_from,
        params.date_to,
        params.date_to,
    ]
    currency = params.currency
    if currency is None:
        currencies = store.query(
            prefix
            + """SELECT currency FROM in_range GROUP BY currency
            ORDER BY count(*) DESC, currency LIMIT 1""",
            args,
        ).to_pylist()
        currency = str(currencies[0]["currency"]) if currencies else None
    return (
        prefix + ", selected AS (SELECT * FROM in_range WHERE currency = ?) ",
        [*args, currency],
        currency,
    )


def _empty(data: pa.Table) -> DigResult:
    return DigResult(
        data,
        None,
        "No matching money sherds in this range yet.",
        None,
        "No chart: there are no matching transactions.",
    )


def _currency_note(currency: str, params: DigParams) -> str:
    return f" Amounts are in {currency}" + (
        ", the currency with the most transactions in this range."
        if params.currency is None
        else "."
    )


def _monthly_bars(
    data: pa.Table,
    value: str,
    series: str,
    order: list[str] | None = None,
    *,
    paired: bool = False,
    title: str,
) -> dict[str, Any]:
    # Time belongs on the x axis; category and merchant labels belong on horizontal bars.
    enc: dict[str, Any] = {
        "x": {
            "field": "bucket",
            "type": "temporal",
            "title": "Local month",
            "axis": {"format": "%b %Y", "labelOverlap": True},
        },
        "y": {"field": value, "type": "quantitative", "title": title},
        "color": {"field": series, "type": "nominal", "title": None},
        "tooltip": [
            {"field": "bucket", "type": "temporal", "format": "%b %Y"},
            {"field": series},
            {"field": value, "type": "quantitative", "format": ".2f"},
        ],
    }
    if order:
        enc["color"]["scale"] = {"domain": order}
    if paired:
        # Ordinal months make offsets discrete and equally spaced.
        enc["x"] = {
            "field": "bucket",
            "type": "ordinal",
            "title": "Local month",
            "axis": {
                "labelExpr": "timeFormat(toDate(datum.value), '%b %Y')",
                "labelAngle": -45,
                "labelOverlap": True,
            },
        }
        enc["xOffset"] = {"field": series, "sort": order}
    if series == "category":
        categories = sorted({str(row[series]) for row in data.to_pylist()})
        ranges = charts.load_theme()["range"]
        if len(categories) > len(ranges["category"]):
            # Use the theme's auxiliary colours before cycling its eight categorical ones.
            palette = list(
                dict.fromkeys(ranges["category"] + ranges["diverging"] + ranges["ordinal"])
            )
            enc["color"]["scale"] = {"domain": categories, "range": palette[: len(categories)]}
    return charts.apply_theme(
        {
            "data": {"values": charts.records(data)},
            "mark": "bar",
            "encoding": enc,
            "width": 640,
            "height": 260,
        }
    )


@dataclass
class MoneyDig:
    id: str
    title: str
    requires: list[str] = field(default_factory=lambda: ["transactions"])

    def compute(self, store: Store, params: DigParams) -> DigResult:
        prefix, args, currency = _selection(store, params)
        metric = self.id.removeprefix("money.")
        note = _currency_note(currency, params) if currency else ""
        if metric == "spend_by_category":
            data = store.query(
                prefix
                + f"""SELECT date_trunc('month', local_ts)::DATE AS bucket,
                category_label AS category, sum({SPEND}) AS spend FROM selected
                WHERE amount < 0 AND kind NOT IN ('exchange','topup') OR kind = 'refund'
                GROUP BY bucket, category_label ORDER BY bucket, category""",
                args,
            )
            if not data.num_rows:
                return _empty(data)
            merchants = store.query(
                prefix
                + f""", totals AS (
                SELECT coalesce(merchant_label, 'Unknown') AS merchant, sum({SPEND}) AS spend
                FROM selected GROUP BY merchant_label HAVING spend != 0)
                SELECT * FROM totals ORDER BY spend DESC, merchant LIMIT ?""",
                [*args, params.top_n],
            ).to_pylist()
            data = data.append_column(
                "top_merchants",
                pa.array(
                    [merchants] * data.num_rows,
                    type=pa.list_(
                        pa.struct([("merchant", pa.string()), ("spend", pa.decimal128(38, 2))])
                    ),
                ),
            )
            totals: dict[str, float] = {}
            for row in data.to_pylist():
                totals[row["category"]] = totals.get(row["category"], 0) + float(row["spend"])
            winner = min(totals, key=lambda category: (-totals[category], category))
            return DigResult(
                data,
                _monthly_bars(data, "spend", "category", title=f"Spend ({currency})"),
                f"Your biggest spending category was {winner}: {totals[winner]:.2f}." + note,
                Headline(winner, round(totals[winner], 2), currency),
                "Monthly net spend by category; refunds reduce their month's spend. "
                f"Top {params.top_n} merchants by net spend over the range are in data." + note,
            )
        if metric == "income_vs_spend":
            data = store.query(
                prefix
                + f""", monthly AS (
                SELECT date_trunc('month', local_ts)::DATE AS bucket, sum({INCOME}) AS income,
                    sum({SPEND}) AS spend FROM selected GROUP BY bucket)
                SELECT *, (income - spend)::DOUBLE / nullif(income, 0) AS savings_rate
                FROM monthly ORDER BY bucket""",
                args,
            )
            if not data.num_rows:
                return _empty(data)
            rates = [r for r in data["savings_rate"].to_pylist() if r is not None]
            rate = sum(rates) / len(rates) if rates else None
            values = pa.Table.from_pylist(
                [
                    {"bucket": r["bucket"], "side": side, "amount": r[side]}
                    for r in data.to_pylist()
                    for side in ("income", "spend")
                ]
            )
            return DigResult(
                data,
                _monthly_bars(
                    values,
                    "amount",
                    "side",
                    ["income", "spend"],
                    paired=True,
                    title=f"Amount ({currency})",
                ),
                (
                    f"Your average monthly savings rate was {rate:.1%}."
                    if rate is not None
                    else "Your savings rate is unavailable because there is no income."
                )
                + note,
                Headline(
                    "Average savings rate",
                    round(rate * 100, 1) if rate is not None else "No income",
                    "%" if rate is not None else None,
                ),
                "Income and net spend by local month. Savings rates exclude zero-income months."
                + note,
            )
        if metric == "weekday_weekend":
            # Count all calendar days, including those with no spend, and clip partial months.
            data = store.query(
                prefix
                + f""", bounds AS (
                SELECT coalesce(?::DATE, min(local_ts)::DATE) AS first,
                       coalesce(?::DATE, max(local_ts)::DATE) AS last FROM selected
                HAVING count(*) > 0
            ), days AS (
                SELECT day::DATE AS day FROM bounds,
                LATERAL generate_series(first, last, INTERVAL '1 day') AS dates(day)
            ), daily AS (
                SELECT local_ts::DATE AS day, sum({SPEND}) AS spend FROM selected GROUP BY day
            ), counts AS (
                SELECT date_trunc('month', d.day)::DATE AS bucket,
                    CASE WHEN isodow(d.day) >= 6 THEN 'weekend' ELSE 'weekday' END AS side,
                    count(*) AS days, sum(coalesce(spend, 0)) AS spend FROM days d
                LEFT JOIN daily USING (day) GROUP BY bucket, side)
            SELECT *, spend::DOUBLE / days AS average_spend FROM counts ORDER BY bucket, side""",
                [*args, params.date_from, params.date_to],
            )
            if not data.num_rows:
                return _empty(data)
            rows = data.to_pylist()
            averages = {
                side: sum(float(r["spend"]) for r in rows if r["side"] == side)
                / sum(r["days"] for r in rows if r["side"] == side)
                for side in ("weekday", "weekend")
                if any(r["side"] == side for r in rows)
            }
            if len(averages) == 2:
                difference = averages["weekend"] - averages["weekday"]
                narrative = (
                    f"A weekend day cost you {abs(difference):.2f} "
                    + ("more" if difference >= 0 else "less")
                    + " than a weekday day."
                )
                headline = Headline("Weekend minus weekday day", round(difference, 2), currency)
            else:
                narrative = "This range needs both weekday and weekend days for a comparison."
                headline = Headline("Weekend comparison", "Not enough days")
            return DigResult(
                data,
                _monthly_bars(
                    data,
                    "average_spend",
                    "side",
                    ["weekday", "weekend"],
                    paired=True,
                    title=f"Average daily spend ({currency})",
                ),
                narrative + note,
                headline,
                "Average net spend per calendar day, with zero-spend days included; "
                "weekends are Saturday and Sunday. Range-edge months are clipped." + note,
            )
        if metric == "delivery_index":
            data = store.query(
                prefix
                + f""", monthly AS (
                SELECT date_trunc('month', local_ts)::DATE AS bucket, sum({SPEND}) AS spend,
                    sum(CASE WHEN regexp_matches(category_label, '(?i)delivery|takeaway')
                        OR regexp_matches(coalesce(merchant_label, ''), ?) THEN {SPEND}
                        ELSE 0 END) AS delivery_spend FROM selected GROUP BY bucket)
                SELECT *, delivery_spend::DOUBLE / nullif(spend, 0) AS delivery_share
                FROM monthly ORDER BY bucket""",
                [*args, DELIVERY],
            )
            if not data.num_rows:
                return _empty(data)
            rows = data.to_pylist()
            latest = rows[-1]
            previous = next(
                (
                    r
                    for r in rows
                    if r["bucket"].year == latest["bucket"].year - 1
                    and r["bucket"].month == latest["bucket"].month
                ),
                None,
            )
            share = latest["delivery_share"]
            delta = (
                (share - previous["delivery_share"]) * 100
                if share is not None and previous and previous["delivery_share"] is not None
                else None
            )
            narrative = (
                f"Delivery was {share:.1%} of your net spend in {latest['bucket']:%B %Y}."
                if share is not None
                else "Delivery share is unavailable: latest month's net spend is zero."
            )
            narrative += (
                f" That's {abs(delta):.1f} percentage points "
                f"{'higher' if delta >= 0 else 'lower'} than a year earlier."
                if delta is not None
                else " No comparable share is available a year earlier."
            )
            chart = charts.line(data, "bucket", "delivery_share")
            chart["mark"] = {"type": "line", "point": True}
            chart["encoding"]["x"]["title"] = "Local month"
            chart["encoding"]["y"].update({"title": "Delivery share", "axis": {"format": ".0%"}})
            return DigResult(
                data,
                chart,
                narrative + note,
                Headline(
                    "Latest delivery share",
                    round(share * 100, 1) if share is not None else "No net spend",
                    "%" if share is not None else None,
                    delta,
                ),
                "Monthly food-delivery net spend as a share of all net spend; matching categories "
                "or recognised delivery merchants, with refunds applied in their month." + note,
            )
        raise ValueError(f"unknown money metric: {metric}")


DIGS: list[Dig] = [
    MoneyDig("money." + metric, title)
    for metric, title in (
        ("spend_by_category", "Spending by category"),
        ("income_vs_spend", "Income and spending"),
        ("weekday_weekend", "Weekday and weekend spending"),
        ("delivery_index", "Delivery index"),
    )
]
