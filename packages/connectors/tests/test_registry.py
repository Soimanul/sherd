import importlib.metadata
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sherd_connectors
from sherd_connectors import registry
from sherd_connectors.base import DetectResult, ImportContext
from sherd_core import Row


class FakeConnector:
    version = "1"
    fixtures_dir: Path | None = None

    def __init__(self, connector_id: str, confidence: float = 0.0) -> None:
        self.id = connector_id
        self.display_name = connector_id.title()
        self.confidence = confidence

    def detect(self, path: Path) -> DetectResult:
        return DetectResult(self.confidence, "fake")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        return iter(())

    def fixtures(self) -> list[Path]:
        return []


class BrokenDetect(FakeConnector):
    def detect(self, path: Path) -> DetectResult:
        raise RuntimeError("secret content from the export")


class FakeEntryPoint:
    def __init__(self, value: str, connector: object) -> None:
        self.value = value
        self._connector = connector

    def load(self) -> object:
        return self._connector


@pytest.fixture
def entry_points(monkeypatch: pytest.MonkeyPatch) -> list[FakeEntryPoint]:
    points: list[FakeEntryPoint] = []

    def fake(*, group: str) -> list[FakeEntryPoint]:
        assert group == registry.ENTRY_POINT_GROUP
        return points

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    return points


@pytest.fixture
def builtin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An extra directory on `sherd_connectors.__path__`, as if it held built-in subpackages."""
    root = tmp_path / "builtin"
    root.mkdir()
    monkeypatch.setattr(sherd_connectors, "__path__", [*sherd_connectors.__path__, str(root)])
    before = set(sys.modules)
    yield root
    for name in set(sys.modules) - before:
        if name.startswith("sherd_connectors."):
            del sys.modules[name]


def write_builtin(root: Path, name: str, body: str) -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text(body)


CONNECTOR_MODULE = (
    "from pathlib import Path\n"
    "from sherd_connectors.base import DetectResult\n"
    "class _C:\n"
    "    id = {id!r}\n"
    "    version = '1'\n"
    "    display_name = 'Fake'\n"
    "    def detect(self, path): return DetectResult(0.0, 'fake')\n"
    "    def parse(self, path, ctx): return iter(())\n"
    "    def fixtures(self): return []\n"
    "CONNECTOR = _C()\n"
)


def test_discovers_builtin_subpackage(
    builtin_dir: Path, entry_points: list[FakeEntryPoint]
) -> None:
    write_builtin(builtin_dir, "fakeconn", CONNECTOR_MODULE.format(id="fakeconn"))
    write_builtin(builtin_dir, "helpers", "VALUE = 1\n")  # no CONNECTOR: not a connector
    (builtin_dir / "loose.py").write_text(CONNECTOR_MODULE.format(id="loose"))  # not a package
    found = registry.discover()
    assert found["fakeconn"].display_name == "Fake"
    assert "helpers" not in found
    assert "loose" not in found
    assert "synth" not in found


def test_discovers_entry_point(entry_points: list[FakeEntryPoint]) -> None:
    entry_points.append(FakeEntryPoint("thirdparty:CONNECTOR", FakeConnector("thirdparty")))
    assert registry.discover()["thirdparty"].id == "thirdparty"


def test_duplicate_id_is_an_error(builtin_dir: Path, entry_points: list[FakeEntryPoint]) -> None:
    write_builtin(builtin_dir, "fakeconn", CONNECTOR_MODULE.format(id="fakeconn"))
    entry_points.append(FakeEntryPoint("thirdparty:CONNECTOR", FakeConnector("fakeconn")))
    with pytest.raises(registry.RegistryError, match="'fakeconn' is registered twice") as error:
        registry.discover()
    assert "sherd_connectors.fakeconn" in str(error.value)
    assert "thirdparty:CONNECTOR" in str(error.value)


def test_entry_point_without_id_is_an_error(entry_points: list[FakeEntryPoint]) -> None:
    entry_points.append(FakeEntryPoint("broken:thing", object()))
    with pytest.raises(registry.RegistryError, match="broken:thing"):
        registry.discover()


def test_broken_builtin_is_a_registry_error(
    builtin_dir: Path, entry_points: list[FakeEntryPoint]
) -> None:
    write_builtin(builtin_dir, "brokenconn", "raise ImportError('no')\n")
    with pytest.raises(registry.RegistryError, match=r"cannot load sherd_connectors\.brokenconn"):
        registry.discover()


def test_broken_entry_point_is_a_registry_error(entry_points: list[FakeEntryPoint]) -> None:
    class Broken(FakeEntryPoint):
        def load(self) -> object:
            raise ModuleNotFoundError("gone")

    entry_points.append(Broken("gone:CONNECTOR", None))
    with pytest.raises(registry.RegistryError, match="cannot load entry point gone:CONNECTOR"):
        registry.discover()


def test_rank_orders_by_confidence(tmp_path: Path) -> None:
    connectors = {
        "low": FakeConnector("low", 0.2),
        "high": FakeConnector("high", 0.9),
        "mid": FakeConnector("mid", 0.6),
    }
    ranked = registry.rank(tmp_path, connectors)
    assert [(c.id, r.confidence) for c, r in ranked] == [("high", 0.9), ("mid", 0.6), ("low", 0.2)]


def test_rank_uses_discovered_connectors(
    tmp_path: Path, entry_points: list[FakeEntryPoint]
) -> None:
    entry_points.append(FakeEntryPoint("x:C", FakeConnector("thirdparty", 0.7)))
    best, result = registry.rank(tmp_path)[0]
    assert (best.id, result.confidence) == ("thirdparty", 0.7)


def test_raising_detect_ranks_zero_and_logs_no_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    connectors = {"broken": BrokenDetect("broken", 0.99), "ok": FakeConnector("ok", 0.3)}
    with caplog.at_level(logging.WARNING, logger="sherd.connectors"):
        ranked = registry.rank(tmp_path, connectors)
    assert [(c.id, r.confidence) for c, r in ranked] == [("ok", 0.3), ("broken", 0.0)]
    assert "broken" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "secret" not in ranked[1][1].reason
