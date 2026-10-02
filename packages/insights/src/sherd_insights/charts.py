"""Small Vega-Lite builders; all visual styling belongs to the packaged theme."""

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any, cast

import pyarrow as pa

Records = pa.Table | Sequence[Mapping[str, Any]]
SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def load_theme() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(files("sherd_insights").joinpath("theme/sherd.vega.json").read_text()),
    )


def apply_theme(spec: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(spec)
    result["$schema"] = SCHEMA
    result["config"] = load_theme()
    return result


def json_value(value: Any) -> Any:
    """Convert Arrow's Python values without losing nested records."""
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_value(v) for v in value]
    return value


def records(data: Records) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        json_value(data.to_pylist() if isinstance(data, pa.Table) else list(data)),
    )


def _spec(data: Records, mark: str, encoding: dict[str, Any]) -> dict[str, Any]:
    return apply_theme(
        {
            "data": {"values": records(data)},
            "mark": mark,
            "encoding": encoding,
            "width": 480,
            "height": 240,
        }
    )


def bar(
    data: Records, x: str, y: str, *, series: str | None = None, paired: bool = False
) -> dict[str, Any]:
    enc: dict[str, Any] = {
        "x": {"field": x, "type": "nominal"},
        "y": {"field": y, "type": "quantitative"},
    }
    if series:
        enc["color"] = {"field": series, "type": "nominal"}
        if paired:
            enc["xOffset"] = {"field": series}
    return _spec(data, "bar", enc)


def line(data: Records, x: str, y: str, *, series: str | None = None) -> dict[str, Any]:
    enc: dict[str, Any] = {
        "x": {"field": x, "type": "temporal"},
        "y": {"field": y, "type": "quantitative"},
    }
    if series:
        enc["color"] = {"field": series, "type": "nominal"}
    return _spec(data, "line", enc)


def heatmap(data: Records, x: str, y: str, value: str) -> dict[str, Any]:
    return _spec(
        data,
        "rect",
        {
            "x": {"field": x, "type": "ordinal"},
            "y": {"field": y, "type": "ordinal"},
            "color": {"field": value, "type": "quantitative"},
        },
    )


def bump(data: Records, x: str, rank: str, series: str) -> dict[str, Any]:
    return _spec(
        data,
        "line",
        {
            "x": {"field": x, "type": "ordinal"},
            "y": {"field": rank, "type": "quantitative", "scale": {"reverse": True, "zero": False}},
            "color": {"field": series, "type": "nominal"},
            "order": {"field": x},
        },
    )


def histogram(data: Records, field: str) -> dict[str, Any]:
    return _spec(
        data,
        "bar",
        {
            "x": {"field": field, "type": "quantitative", "bin": True},
            "y": {"aggregate": "count", "type": "quantitative"},
        },
    )
