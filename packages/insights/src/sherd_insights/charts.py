"""Small Vega-Lite builders; all visual styling belongs to the packaged theme."""

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib.resources import files
from math import ceil
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
    data: Records,
    x: str,
    y: str,
    *,
    series: str | None = None,
    paired: bool = False,
    series_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Horizontal bars keep every categorical label readable."""
    enc: dict[str, Any] = {
        "y": {
            "field": x,
            "type": "nominal",
            "sort": None,
            "axis": {"labelOverlap": False, "labelLimit": 240},
        },
        "x": {"field": y, "type": "quantitative"},
    }
    if series:
        enc["color"] = {"field": series, "type": "nominal"}
        if series_order:
            enc["color"]["scale"] = {"domain": list(series_order)}
        if paired:
            enc["yOffset"] = {"field": series, "sort": list(series_order) if series_order else None}
    spec = _spec(data, "bar", enc)
    categories = len({row[x] for row in spec["data"]["values"]})
    spec["height"] = max(240, categories * (40 if paired else 28))
    return spec


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
    values = records(data)
    # Separate runs prevent lines bridging years outside the top contacts.
    runs: list[dict[str, Any]] = []
    ends: list[dict[str, Any]] = []
    contacts = list(dict.fromkeys(row[series] for row in values))
    for contact in contacts:
        points = sorted((row for row in values if row[series] == contact), key=lambda row: row[x])
        run = 0
        previous: Any = None
        for point in points:
            if previous is not None and point[x] != previous + 1:
                run += 1
            runs.append({**point, "__run": f"{contact}:{run}"})
            previous = point[x]
        ends.append(points[-1])
    ranks = list(range(1, max((ceil(row[rank]) for row in values), default=1) + 1))
    return apply_theme(
        {
            "data": {"values": values},
            "width": 480,
            "height": 240,
            "padding": {"left": 4, "top": 4, "bottom": 4, "right": 180},
            "encoding": {
                "x": {"field": x, "type": "ordinal", "title": None},
                "y": {"field": rank, "type": "ordinal", "scale": {"domain": ranks}},
                "color": {
                    "field": series,
                    "type": "nominal",
                    "legend": None,
                    "scale": {"domain": contacts},
                },
                "tooltip": [{"field": series}, {"field": x}, {"field": rank}],
            },
            "layer": [
                {
                    "data": {"values": runs},
                    "mark": "line",
                    "encoding": {"detail": {"field": "__run"}, "order": {"field": x}},
                },
                {"mark": "point"},
                {
                    "data": {"values": ends},
                    "mark": {"type": "text", "style": "label", "align": "left", "dx": 10},
                    "encoding": {"text": {"field": series}},
                },
            ],
        }
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
