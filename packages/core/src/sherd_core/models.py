"""Canonical row models, one per fact table (PLAN §4.2, §4.4).

Connectors fill `source_file`, `source_row_id`, `meta` and the table's own columns; `id`,
`source`, `import_id` and `imported_at` are set by the store.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

_CENT = Decimal("0.01")
_MONEY_LIMIT = Decimal(10) ** 16  # DECIMAL(18,2) holds 16 integer digits


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _to_money(value: Decimal) -> Decimal:
    quantised = value.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    if abs(quantised) >= _MONEY_LIMIT:
        raise ValueError("amount does not fit DECIMAL(18,2)")
    return quantised


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime (naive values are rejected), normalised to UTC."""

Money = Annotated[Decimal, AfterValidator(_to_money)]
"""A finite decimal quantised to 2 places (half-even)."""


class _FactRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_name: ClassVar[str]

    source_file: str = Field(min_length=1)
    source_row_id: str = Field(min_length=1)
    meta: dict[str, Any] | None = None


class Message(_FactRow):
    table_name: ClassVar[str] = "messages"

    chat_id: str
    chat_name: str | None = None
    chat_kind: Literal["direct", "group"]
    sender_id: str | None = None
    sender_name: str | None = None
    is_from_me: bool
    contact_id: str | None = None
    ts: UtcDatetime
    text: str | None = None
    kind: Literal["text", "media", "system", "deleted"]
    media_type: Literal["image", "video", "audio", "document", "sticker", "gif", "other"] | None = (
        None
    )
    reply_to_id: str | None = None


class MediaPlay(_FactRow):
    table_name: ClassVar[str] = "media_plays"

    ts: UtcDatetime
    media_kind: Literal["track", "episode", "video", "audiobook"]
    artist: str | None = None
    track: str | None = None
    album: str | None = None
    uri: str | None = None
    ms_played: int
    platform: str | None = None
    shuffle: bool | None = None
    skipped: bool | None = None


class Transaction(_FactRow):
    table_name: ClassVar[str] = "transactions"

    ts: UtcDatetime
    amount: Money
    currency: str
    merchant_raw: str | None = None
    merchant: str | None = None
    counterparty: str | None = None
    contact_id: str | None = None
    category: str | None = None
    account: str
    balance: Money | None = None
    kind: Literal["card", "transfer", "fee", "topup", "exchange", "refund", "other"]


class Event(_FactRow):
    table_name: ClassVar[str] = "events"

    ts: UtcDatetime
    kind: str
    title: str | None = None
    url: str | None = None


class Location(_FactRow):
    table_name: ClassVar[str] = "locations"

    ts: UtcDatetime
    end_ts: UtcDatetime | None = None
    lat: float
    lon: float
    accuracy_m: float | None = None
    place_name: str | None = None
    kind: Literal["ping", "visit", "segment"]


Row = Message | MediaPlay | Transaction | Event | Location

ROW_MODELS: tuple[type[Row], ...] = (Message, MediaPlay, Transaction, Event, Location)
FACT_TABLES: tuple[str, ...] = tuple(model.table_name for model in ROW_MODELS)
