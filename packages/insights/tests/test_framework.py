import importlib.metadata
import json
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from sherd_connectors.synth import import_profile
from sherd_core import Store
from sherd_insights import DigParams, charts, registry
from sherd_insights.digs import messages_metrics

from .conftest import insert, message

IDS = [
    "messages." + name
    for name in (
        "volume_by_contact",
        "top_contacts_by_year",
        "response_times",
        "conversation_starters",
        "activity_heatmap",
        "streaks_silences",
        "emoji_words",
    )
]


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    with Store.open(tmp_path_factory.mktemp("demo") / "demo.duckdb") as store:
        import_profile(store, "demo")
        yield store


def assert_spec(spec: dict[str, Any]) -> None:
    assert spec["$schema"] == charts.SCHEMA
    layers = spec.get("layer", [spec])
    for layer in layers:
        mark = layer["mark"]
        assert (mark["type"] if isinstance(mark, dict) else mark) in (
            "bar",
            "line",
            "rect",
            "point",
            "text",
        )
    assert "x" in spec["encoding"]
    assert "y" in spec["encoding"]
    assert isinstance(spec["data"]["values"], list)
    assert spec["config"] == charts.load_theme()
    json.dumps(spec, allow_nan=False)
    without_theme = {key: value for key, value in spec.items() if key != "config"}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert "font" not in key.lower()
                if key == "color":
                    assert isinstance(child, dict)
                    assert "field" in child
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            assert not value.startswith("#")

    walk(without_theme)


@pytest.mark.parametrize("dig_id", IDS)
def test_demo_each_dig_under_one_second(demo: Store, dig_id: str) -> None:
    dig = registry.discover()[dig_id]
    start = time.perf_counter()
    result = dig.compute(demo, DigParams(tz="Europe/Bucharest"))
    elapsed = time.perf_counter() - start
    assert elapsed < 1, f"{dig_id}: {elapsed:.3f} seconds"
    assert result.data.num_rows > 0
    assert result.chart is not None
    assert result.headline is not None
    assert result.narrative
    assert result.text_summary
    assert_spec(result.chart)
    again = dig.compute(demo, DigParams(tz="Europe/Bucharest"))
    assert again.data.equals(result.data)
    assert again.chart == result.chart
    assert again.headline == result.headline
    assert again.narrative == result.narrative
    assert again.text_summary == result.text_summary


@pytest.mark.parametrize("dig_id", IDS)
def test_empty_database_graceful(store: Store, dig_id: str) -> None:
    got = registry.discover()[dig_id].compute(store, DigParams())
    assert got.data.num_rows == 0
    assert got.chart is None
    assert got.headline is None
    assert got.narrative
    assert got.text_summary


@pytest.mark.parametrize("dig_id", IDS)
def test_empty_date_range_graceful(demo: Store, dig_id: str) -> None:
    got = registry.discover()[dig_id].compute(demo, DigParams(date_from=date(2099, 1, 1)))
    assert got.data.num_rows == 0
    assert got.chart is None


def test_available_and_discovery(store: Store) -> None:
    assert set(IDS) <= set(registry.discover())
    assert registry.available(store) == []
    insert(store, [message("1", "2024-01-01T00:00:00+00:00")])
    assert {dig.id for dig in registry.available(store)} == set(IDS)


def test_plugin_and_duplicate_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = messages_metrics.MessageDig("plugin.example", "Example", ["events"])
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kwargs: [EntryPoint(name="example", value="plugin:dig", group="sherd.digs")],
    )
    monkeypatch.setattr(EntryPoint, "load", lambda self: plugin)
    assert registry.discover()["plugin.example"] is plugin
    plugin.id = "messages.volume_by_contact"
    with pytest.raises(registry.RegistryError, match="registered twice"):
        registry.discover()


def test_available_requires_all_tables(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    insert(store, [message("1", "2024-01-01T00:00:00+00:00")])
    plugin = messages_metrics.MessageDig("plugin.example", "Example", ["messages", "events"])
    monkeypatch.setattr(registry, "discover", lambda: {plugin.id: plugin})
    assert registry.available(store) == []


def test_theme_exact_and_no_input_mutation() -> None:
    path = Path(messages_metrics.__file__).parent.parent / "theme" / "sherd.vega.json"
    spec: dict[str, Any] = {"mark": "bar", "config": {"bad": True}}
    themed = charts.apply_theme(spec)
    assert themed["config"] == json.loads(path.read_text())
    assert spec == {"mark": "bar", "config": {"bad": True}}
    themed["config"]["font"] = "changed"
    assert charts.load_theme()["font"] == "Fira Sans"


@pytest.mark.parametrize("arrow", [True, False])
def test_builders_json_values_and_theme(arrow: bool) -> None:
    data = [{"bucket": date(2024, 1, 1), "amount": Decimal("1.25"), "contact": "Contact A"}]
    source = pa.Table.from_pylist(data) if arrow else data
    specs = [
        charts.bar(source, "contact", "amount", series="contact", paired=True),
        charts.line(source, "bucket", "amount", series="contact"),
        charts.heatmap(source, "bucket", "contact", "amount"),
        charts.bump(source, "bucket", "amount", "contact"),
        charts.histogram(source, "amount"),
    ]
    for spec in specs:
        assert_spec(spec)
        assert spec["data"]["values"] == [
            {"bucket": "2024-01-01", "amount": 1.25, "contact": "Contact A"}
        ]


def test_available_ledger_and_missing_table(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    insert(store, [message("1", "2024-01-01T00:00:00+00:00")])
    present = messages_metrics.MessageDig("plugin.ledger", "Ledger", ["messages", "imports"])
    missing = messages_metrics.MessageDig(
        "plugin.missing", "Missing", ['missing"; DROP TABLE messages']
    )
    monkeypatch.setattr(registry, "discover", lambda: {present.id: present, missing.id: missing})
    assert registry.available(store) == [present]
    assert store.table_counts()["messages"] == 1
