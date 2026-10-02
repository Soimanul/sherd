"""Contract C: deterministic, read-only insights."""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

import pyarrow as pa
from sherd_core import Store


@dataclass(frozen=True)
class DigParams:
    date_from: date | None = None
    date_to: date | None = None
    top_n: int = 10
    granularity: Literal["day", "week", "month", "year"] = "month"
    tz: str = "UTC"
    currency: str | None = None


@dataclass(frozen=True)
class Headline:
    label: str
    value: float | int | str
    unit: str | None = None
    delta: float | None = None


@dataclass(frozen=True)
class DigResult:
    data: pa.Table
    chart: dict[str, Any] | None
    narrative: str
    headline: Headline | None
    text_summary: str


class Dig(Protocol):
    id: str
    title: str
    requires: list[str]

    def compute(self, store: Store, params: DigParams) -> DigResult: ...
