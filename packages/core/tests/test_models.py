from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sherd_core.models import ROW_MODELS, Event, Location, MediaPlay, Message, Transaction

TS = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)

VALID: dict[type, dict[str, Any]] = {
    Message: {
        "chat_id": "c",
        "chat_kind": "group",
        "is_from_me": False,
        "ts": TS,
        "kind": "media",
        "media_type": "image",
    },
    MediaPlay: {"ts": TS, "media_kind": "track", "ms_played": 1000},
    Transaction: {
        "ts": TS,
        "amount": "-1.50",
        "currency": "EUR",
        "account": "main",
        "kind": "card",
    },
    Event: {"ts": TS, "kind": "youtube.watch"},
    Location: {"ts": TS, "lat": 44.4, "lon": 26.1, "kind": "ping"},
}


def build(model: type, **overrides: Any) -> Any:
    fields = {"source_file": "f.json", "source_row_id": "r1", **VALID[model], **overrides}
    return model(**fields)


def test_every_model_has_a_valid_example_and_table_name() -> None:
    assert {model.table_name for model in ROW_MODELS} == {
        "messages",
        "media_plays",
        "transactions",
        "events",
        "locations",
    }
    for model in ROW_MODELS:
        build(model)


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (Message, "chat_kind", "channel"),
        (Message, "kind", "image"),
        (Message, "media_type", "photo"),
        (MediaPlay, "media_kind", "song"),
        (Transaction, "kind", "payment"),
        (Location, "kind", "stop"),
    ],
)
def test_enum_violations_rejected(model: type, field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        build(model, **{field: value})


@pytest.mark.parametrize(
    ("model", "field"),
    [(Message, "chat_id"), (MediaPlay, "ms_played"), (Transaction, "account"), (Location, "lat")],
)
def test_required_columns_rejected_when_missing(model: type, field: str) -> None:
    fields = {k: v for k, v in VALID[model].items() if k != field}
    with pytest.raises(ValidationError, match=field):
        model(source_file="f", source_row_id="r", **fields)


@pytest.mark.parametrize("field", ["source_file", "source_row_id"])
def test_provenance_must_be_non_empty(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        build(Event, **{field: ""})


@pytest.mark.parametrize("field", ["id", "source", "import_id", "imported_at", "unknown"])
def test_store_set_and_unknown_fields_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(Event, **{field: "x"})


@pytest.mark.parametrize("model", ROW_MODELS)
def test_naive_datetime_rejected(model: type) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        build(model, ts=datetime(2024, 3, 1, 9, 30))


def test_naive_end_ts_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        build(Location, end_ts=datetime(2024, 3, 1, 10, 0))


def test_aware_datetime_normalised_to_utc() -> None:
    local = datetime(2024, 3, 1, 12, 30, tzinfo=timezone(timedelta(hours=3)))
    row = build(Location, ts=local, end_ts="2024-03-01T13:00:00+03:00")
    assert row.ts == local
    assert row.ts.utcoffset() == timedelta(0)
    assert (row.ts.hour, row.end_ts.hour) == (9, 10)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("-12.345", "-12.34"), ("-12.355", "-12.36"), ("7", "7.00"), (Decimal("0.1"), "0.10")],
)
def test_money_quantised_to_cents(raw: object, expected: str) -> None:
    row = build(Transaction, amount=raw, balance=raw)
    assert str(row.amount) == expected
    assert str(row.balance) == expected


@pytest.mark.parametrize("field", ["amount", "balance"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e100", "1e16", "-10000000000000000"])
def test_money_must_fit_decimal_18_2(field: str, raw: str) -> None:
    with pytest.raises(ValidationError, match=field):
        build(Transaction, **{field: raw})


def test_rows_are_immutable() -> None:
    row = build(Event)
    with pytest.raises(ValidationError, match="frozen"):
        row.kind = "other.kind"
