"""Exact single-currency money contracts and synth subscription precision/recall."""

import json
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.synth import data as synth_data
from sherd_connectors.synth import import_profile
from sherd_core import Store, Transaction
from sherd_insights import DigParams, DigResult, charts
from sherd_insights.registry import discover

IDS = [
    "money." + metric
    for metric in (
        "spend_by_category",
        "subscriptions",
        "income_vs_spend",
        "weekday_weekend",
        "delivery_index",
    )
]


def txn(key: str, ts: str, amount: str, **extra: Any) -> Transaction:
    fields: dict[str, Any] = {
        "source_file": "synthetic.csv",
        "source_row_id": key,
        "ts": datetime.fromisoformat(ts),
        "amount": Decimal(amount),
        "currency": "RON",
        "account": "synthetic",
        "merchant": "Merchant A",
        "category": "groceries",
        "kind": "card",
    }
    fields.update(extra)
    return Transaction(**fields)


def insert(store: Store, rows: list[Transaction]) -> None:
    key = store.begin_import("synthetic", "1", "synthetic", "UTC")
    store.upsert(key, "synthetic", rows)
    store.finish_import(key, "succeeded")


def compute(store: Store, metric: str, params: DigParams | None = None) -> DigResult:
    return discover()["money." + metric].compute(store, params or DigParams())


def test_currency_parameter_default_and_explicit(store: Store) -> None:
    assert DigParams().currency is None
    assert DigParams(currency="EUR").currency == "EUR"
    insert(
        store,
        [
            txn("1", "2024-01-01T12:00:00+00:00", "-20"),
            txn("2", "2024-01-02T12:00:00+00:00", "-30"),
            txn("3", "2024-01-03T12:00:00+00:00", "-999", currency="EUR"),
        ],
    )
    default = compute(store, "income_vs_spend")
    assert default.data.to_pylist() == [
        {
            "bucket": date(2024, 1, 1),
            "income": Decimal("0"),
            "spend": Decimal("50"),
            "savings_rate": None,
        }
    ]
    assert "RON, the currency with the most transactions" in default.narrative
    explicit = compute(store, "income_vs_spend", DigParams(currency="EUR"))
    assert explicit.data.to_pylist()[0]["spend"] == Decimal("999")
    assert "EUR" in explicit.narrative
    assert "most transactions" not in explicit.narrative
    assert compute(store, "income_vs_spend", DigParams(currency="USD")).chart is None


def test_spend_category_refund_month_exclusions_null_and_merchant_fallback(store: Store) -> None:
    insert(
        store,
        [
            txn(
                "1", "2024-01-31T22:30:00+00:00", "-100", merchant=None, merchant_raw="Raw Merchant"
            ),
            txn("2", "2024-02-02T12:00:00+00:00", "-30", category=None),
            txn(
                "3",
                "2024-03-01T12:00:00+00:00",
                "20",
                kind="refund",
                merchant=None,
                merchant_raw="Raw Merchant",
            ),
            txn("4", "2024-02-02T12:00:00+00:00", "-999", kind="exchange"),
            txn("5", "2024-02-02T12:00:00+00:00", "-999", kind="topup"),
            txn("6", "2024-02-02T12:00:00+00:00", "999", kind="transfer"),
            txn("7", "2024-02-02T12:00:00+00:00", "-999", currency="EUR"),
        ],
    )
    got = compute(store, "spend_by_category", DigParams(top_n=1, tz="Europe/Bucharest"))
    merchants = [{"merchant": "Raw Merchant", "spend": Decimal("80.00")}]
    assert got.data.to_pylist() == [
        {
            "bucket": date(2024, 2, 1),
            "category": "Uncategorised",
            "spend": Decimal("30"),
            "top_merchants": merchants,
        },
        {
            "bucket": date(2024, 2, 1),
            "category": "groceries",
            "spend": Decimal("100"),
            "top_merchants": merchants,
        },
        {
            "bucket": date(2024, 3, 1),
            "category": "groceries",
            "spend": Decimal("-20"),
            "top_merchants": merchants,
        },
    ]
    assert got.headline
    assert (got.headline.label, got.headline.value, got.headline.unit) == (
        "groceries",
        80,
        "RON",
    )


def test_income_spend_savings_monthly_average_and_zero_income(store: Store) -> None:
    insert(
        store,
        [
            txn("1", "2024-01-01T12:00:00+00:00", "100", kind="transfer"),
            txn("2", "2024-01-02T12:00:00+00:00", "-60"),
            txn("3", "2024-01-03T12:00:00+00:00", "10", kind="refund"),
            txn("4", "2024-01-04T12:00:00+00:00", "999", kind="exchange"),
            txn("5", "2024-01-04T12:00:00+00:00", "999", kind="topup"),
            txn("6", "2024-02-01T12:00:00+00:00", "200"),
            txn("7", "2024-02-02T12:00:00+00:00", "-150"),
            txn("8", "2024-03-01T12:00:00+00:00", "-20"),
        ],
    )
    got = compute(store, "income_vs_spend")
    assert got.data.to_pylist() == [
        {
            "bucket": date(2024, 1, 1),
            "income": Decimal("100"),
            "spend": Decimal("50"),
            "savings_rate": 0.5,
        },
        {
            "bucket": date(2024, 2, 1),
            "income": Decimal("200"),
            "spend": Decimal("150"),
            "savings_rate": 0.25,
        },
        {
            "bucket": date(2024, 3, 1),
            "income": Decimal("0"),
            "spend": Decimal("20"),
            "savings_rate": None,
        },
    ]
    assert got.headline
    assert got.headline.value == 37.5
    assert got.chart
    assert got.chart["encoding"]["color"]["scale"]["domain"] == ["income", "spend"]


def test_weekday_weekend_calendar_denominators_partial_months_local_and_zero_days(
    store: Store,
) -> None:
    insert(
        store,
        [
            txn("1", "2024-01-05T22:30:00+00:00", "-40"),  # Saturday local
            txn("2", "2024-01-08T10:00:00+00:00", "-20"),
            txn("3", "2024-01-07T10:00:00+00:00", "10", kind="refund"),
            txn("4", "2024-02-03T12:00:00+00:00", "-99"),
        ],
    )
    got = compute(
        store,
        "weekday_weekend",
        DigParams(date(2024, 1, 5), date(2024, 1, 8), tz="Europe/Bucharest"),
    )
    assert got.data.to_pylist() == [
        {
            "bucket": date(2024, 1, 1),
            "side": "weekday",
            "days": 2,
            "spend": Decimal("20"),
            "average_spend": 10.0,
        },
        {
            "bucket": date(2024, 1, 1),
            "side": "weekend",
            "days": 2,
            "spend": Decimal("30"),
            "average_spend": 15.0,
        },
    ]
    assert got.headline
    assert got.headline.value == 5.0
    single = compute(store, "weekday_weekend", DigParams(date(2024, 1, 8), date(2024, 1, 8)))
    assert single.headline
    assert single.headline.value == "Not enough days"
    whole = compute(store, "weekday_weekend", DigParams(date(2024, 1, 1), date(2024, 1, 31)))
    assert {r["side"]: r["days"] for r in whole.data.to_pylist()} == {"weekday": 23, "weekend": 8}


def test_delivery_category_merchants_refunds_and_exact_year_comparison(store: Store) -> None:
    insert(
        store,
        [
            txn("1", "2023-02-01T12:00:00+00:00", "-20", merchant="Glovo"),
            txn("2", "2023-02-01T12:30:00+00:00", "-80"),
            txn("3", "2024-01-31T22:30:00+00:00", "-30", category="TAKEAWAY"),
            txn("4", "2024-02-01T12:00:00+00:00", "-20", merchant="Uber Eats"),
            txn("5", "2024-02-01T13:00:00+00:00", "10", merchant="Uber Eats", kind="refund"),
            txn("6", "2024-02-01T14:00:00+00:00", "-60"),
            txn("7", "2024-02-01T14:00:00+00:00", "-500", merchant="Glovo", currency="EUR"),
        ],
    )
    got = compute(store, "delivery_index", DigParams(tz="Europe/Bucharest"))
    assert got.data.to_pylist() == [
        {
            "bucket": date(2023, 2, 1),
            "spend": Decimal("100"),
            "delivery_spend": Decimal("20"),
            "delivery_share": 0.2,
        },
        {
            "bucket": date(2024, 2, 1),
            "spend": Decimal("100"),
            "delivery_spend": Decimal("40"),
            "delivery_share": 0.4,
        },
    ]
    assert got.headline
    assert got.headline.value == 40.0
    assert got.headline.delta == 20.0
    assert "20.0 percentage points higher" in got.narrative


@pytest.mark.parametrize(
    "merchant",
    [
        "Glovo",
        "Tazz",
        "Bolt Food",
        "Uber Eats",
        "Deliveroo",
        "Wolt",
        "Just Eat",
        "foodpanda",
        "DoorDash",
        "Takeaway",
        "Lieferando",
    ],
)
def test_builtin_delivery_merchants_case_insensitive_fallback(store: Store, merchant: str) -> None:
    insert(
        store,
        [
            txn(
                "1",
                "2024-01-01T12:00:00+00:00",
                "-10",
                merchant=None,
                merchant_raw=merchant.upper(),
            )
        ],
    )
    assert compute(store, "delivery_index").data.to_pylist()[0]["delivery_share"] == 1.0


def test_subscriptions_monthly_yearly_active_amount_near_miss_and_price_history(
    store: Store,
) -> None:
    rows = [
        txn(f"m{i}", f"2024-{month:02}-01T12:00:00+00:00", amount, merchant="Netflix")
        for i, (month, amount) in enumerate(
            ((1, "-49.99"), (2, "-49.99"), (3, "-49.99"), (4, "-59.99"))
        )
    ]
    rows += [
        txn(f"y{year}", f"{year}-04-01T12:00:00+00:00", "-120", merchant="Yearly")
        for year in (2022, 2023, 2024)
    ]
    rows += [
        txn(f"n{month}", f"2024-{month:02}-01T12:00:00+00:00", amount, merchant="Irregular")
        for month, amount in ((1, "-10"), (2, "-40"), (3, "-20"))
    ]
    rows += [
        txn(f"i{month}", f"2023-{month:02}-01T12:00:00+00:00", "-8", merchant="Inactive")
        for month in (1, 2, 3)
    ]
    rows += [
        txn(f"s{i}", ts, "-10", merchant="Short cadence")
        for i, ts in enumerate(
            ("2024-01-01T00:00:00+00:00", "2024-01-05T00:00:00+00:00", "2024-01-10T00:00:00+00:00")
        )
    ]
    insert(store, rows)
    got = compute(store, "subscriptions", DigParams(date_to=date(2024, 4, 30)))
    assert got.data.to_pylist() == [
        {
            "merchant": "Netflix",
            "period": "monthly",
            "typical_amount": Decimal("59.99"),
            "annual_cost": Decimal("719.88"),
            "first_charge": date(2024, 1, 1),
            "last_charge": date(2024, 4, 1),
            "active": True,
            "next_expected_date": date(2024, 5, 2),
            "interval_days": 31.0,
            "charges": 4,
            "price_history": [
                {
                    "first_charge": date(2024, 1, 1),
                    "last_charge": date(2024, 3, 1),
                    "amount": Decimal("49.99"),
                },
                {
                    "first_charge": date(2024, 4, 1),
                    "last_charge": date(2024, 4, 1),
                    "amount": Decimal("59.99"),
                },
            ],
        },
        {
            "merchant": "Yearly",
            "period": "yearly",
            "typical_amount": Decimal("120"),
            "annual_cost": Decimal("120"),
            "first_charge": date(2022, 4, 1),
            "last_charge": date(2024, 4, 1),
            "active": True,
            "next_expected_date": date(2025, 4, 2),
            "interval_days": 365.5,
            "charges": 3,
            "price_history": [
                {
                    "first_charge": date(2022, 4, 1),
                    "last_charge": date(2024, 4, 1),
                    "amount": Decimal("120"),
                }
            ],
        },
        {
            "merchant": "Inactive",
            "period": "monthly",
            "typical_amount": Decimal("8"),
            "annual_cost": Decimal("96"),
            "first_charge": date(2023, 1, 1),
            "last_charge": date(2023, 3, 1),
            "active": False,
            "next_expected_date": date(2023, 3, 31),
            "interval_days": 29.5,
            "charges": 3,
            "price_history": [
                {
                    "first_charge": date(2023, 1, 1),
                    "last_charge": date(2023, 3, 1),
                    "amount": Decimal("8"),
                }
            ],
        },
    ]
    assert got.headline
    assert got.headline.value == 839.88
    assert "changed price" in got.narrative
    assert got.chart
    assert got.chart["encoding"]["y"]["field"] == "merchant"


def test_subscription_amount_tolerance_and_recent_local_open_activity(store: Store) -> None:
    today = datetime.now(ZoneInfo("Europe/Bucharest")).date()
    # Three recent, 30-day charges; exact +/-10% edges stay in one run.
    from datetime import timedelta

    insert(
        store,
        [
            txn(
                str(i),
                datetime.combine(
                    today - timedelta(days=60 - i * 30),
                    datetime.min.time(),
                    tzinfo=ZoneInfo("Europe/Bucharest"),
                ).isoformat(),
                amount,
                merchant="Stable",
            )
            for i, amount in enumerate(("-9", "-11", "-10"))
        ],
    )
    got = compute(store, "subscriptions", DigParams(tz="Europe/Bucharest"))
    row = got.data.to_pylist()[0]
    assert row["active"] is True
    assert row["typical_amount"] == Decimal("10")
    assert len(row["price_history"]) == 1
    assert row["next_expected_date"] == today + timedelta(days=30)
    assert got.headline
    assert got.headline.value == 120


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    with Store.open(tmp_path_factory.mktemp("money-demo") / "demo.duckdb") as store:
        import_profile(store, "demo")
        yield store


def test_demo_subscription_precision_recall_all_planted_currencies_and_price_change(
    demo: Store,
) -> None:
    for currency in ("RON", "EUR"):
        got = compute(
            demo, "subscriptions", DigParams(date_to=date(2025, 12, 31), currency=currency)
        )
        rows = got.data.to_pylist()
        merchants = {r["merchant"] for r in rows}
        expected = {s.merchant for s in synth_data.SUBSCRIPTIONS if s.currency == currency}
        assert expected <= merchants
        extras = {"Revolut Premium"} if currency == "RON" else set()  # planted monthly fee
        assert merchants == expected | extras
        assert not merchants & {m[0] for m in synth_data.CARD_MERCHANTS}
        assert len(rows) == len(merchants)
        if currency == "RON":
            netflix = next(r for r in rows if r["merchant"] == "Netflix")
            assert netflix["active"] is True
            assert netflix["typical_amount"] == Decimal("59.99")
            assert [h["amount"] for h in netflix["price_history"]] == [
                Decimal("49.99"),
                Decimal("59.99"),
            ]


@pytest.mark.parametrize("dig_id", IDS)
def test_demo_under_one_second_deterministic_and_empty_range(demo: Store, dig_id: str) -> None:
    dig = discover()[dig_id]
    params = DigParams(date_to=date(2025, 12, 31), tz="Europe/Bucharest")
    start = time.perf_counter()
    got = dig.compute(demo, params)
    assert time.perf_counter() - start < 1
    assert got.data.num_rows
    assert got.chart
    assert got.headline
    assert got.narrative
    assert got.text_summary
    assert got.chart["config"] == charts.load_theme()
    json.dumps(got.chart, allow_nan=False)
    again = dig.compute(demo, params)
    assert got.data.equals(again.data)
    assert got.chart == again.chart
    assert (got.headline, got.narrative, got.text_summary) == (
        again.headline,
        again.narrative,
        again.text_summary,
    )
    empty = dig.compute(demo, DigParams(date_from=date(2099, 1, 1)))
    assert empty.data.num_rows == 0
    assert empty.chart is None
    assert empty.headline is None


@pytest.mark.parametrize("dig_id", IDS)
def test_empty_database(store: Store, dig_id: str) -> None:
    got = discover()[dig_id].compute(store, DigParams())
    assert got.data.num_rows == 0
    assert got.chart is None
    assert got.headline is None
    assert got.narrative
    assert got.text_summary


def test_category_top_seven_other_human_labels_and_totals(store: Store) -> None:
    insert(
        store,
        [
            txn(
                str(i),
                "2024-01-01T12:00:00+00:00",
                str(-100 + i),
                category="food_delivery" if i == 0 else f"category_{i}",
            )
            for i in range(10)
        ],
    )
    got = compute(store, "spend_by_category")
    assert got.chart
    values = got.chart["data"]["values"]
    labels = {r["category"] for r in values}
    assert labels == {"Food delivery", "Other"} | {f"Category {i}" for i in range(1, 7)}
    assert next(r["spend"] for r in values if r["category"] == "Other") == 276
    assert sum(r["spend"] for r in values) == 955
    scale = got.chart["encoding"]["color"]["scale"]
    assert set(scale["domain"]) == labels
    assert len(set(scale["range"])) == 8
    assert scale["range"] == charts.load_theme()["range"]["category"]


@pytest.mark.parametrize(
    ("interval", "accepted"),
    [
        (24, False),
        (25, True),
        (35, True),
        (36, False),
        (349, False),
        (350, True),
        (380, True),
        (381, False),
    ],
)
def test_subscription_cadence_window_edges(store: Store, interval: int, accepted: bool) -> None:
    start = date(2020, 1, 1)
    insert(
        store,
        [
            txn(str(i), f"{start + timedelta(days=i * interval)}T12:00:00+00:00", "-10")
            for i in range(3)
        ],
    )
    got = compute(store, "subscriptions", DigParams(date_to=start + timedelta(days=interval * 2)))
    assert bool(got.data.num_rows) is accepted


@pytest.mark.parametrize(
    ("interval", "elapsed", "active"),
    [(30, 45, True), (30, 46, False), (350, 525, True), (350, 526, False)],
)
def test_subscription_active_observed_period_boundary(
    store: Store, interval: int, elapsed: int, active: bool
) -> None:
    start = date(2020, 1, 1)
    last = start + timedelta(days=interval * 2)
    insert(
        store,
        [
            txn(str(i), f"{start + timedelta(days=i * interval)}T12:00:00+00:00", "-10")
            for i in range(3)
        ],
    )
    got = compute(store, "subscriptions", DigParams(date_to=last + timedelta(days=elapsed)))
    assert got.data.to_pylist()[0]["active"] is active
    assert got.chart
    scale = got.chart["encoding"]["color"]["scale"]
    assert scale["domain"] == ["active", "inactive"]
    assert scale["range"][1] == charts.load_theme()["axis"]["labelColor"] == "#aea49c"


@pytest.mark.parametrize(
    ("latest", "accepted"),
    [("15", True), ("15.01", False), ("5", True), ("4.99", False), ("50", False)],
)
def test_subscription_consecutive_price_median_fifty_percent_limit(
    store: Store, latest: str, accepted: bool
) -> None:
    insert(
        store,
        [
            txn(str(i), f"2024-{i + 1:02}-01T12:00:00+00:00", "-" + amount)
            for i, amount in enumerate(("10", "10", "10", latest))
        ],
    )
    got = compute(store, "subscriptions", DigParams(date_to=date(2024, 4, 30)))
    assert bool(got.data.num_rows) is accepted


@pytest.mark.parametrize("kind", ["fee", "transfer"])
def test_subscription_recurring_negative_fees_and_transfers(store: Store, kind: str) -> None:
    insert(
        store,
        [txn(str(i), f"2024-{i + 1:02}-01T12:00:00+00:00", "-39.99", kind=kind) for i in range(3)],
    )
    got = compute(store, "subscriptions", DigParams(date_to=date(2024, 3, 31)))
    assert got.data.to_pylist()[0]["annual_cost"] == Decimal("479.88")


@pytest.mark.parametrize(
    ("amounts", "accepted"),
    [
        (("9", "10", "11", "15"), True),
        (("10", "10", "10", "15", "15", "15", "22.50"), True),
        (("10", "10", "10", "15", "15", "15", "23"), False),
    ],
)
def test_subscription_price_limit_uses_each_consecutive_run_median(
    store: Store, amounts: tuple[str, ...], accepted: bool
) -> None:
    insert(
        store,
        [
            txn(str(i), f"2024-{i + 1:02}-01T12:00:00+00:00", "-" + amount)
            for i, amount in enumerate(amounts)
        ],
    )
    got = compute(store, "subscriptions", DigParams(date_to=date(2024, 7, 31)))
    assert bool(got.data.num_rows) is accepted
