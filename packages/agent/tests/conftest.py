from collections.abc import Iterator
from pathlib import Path

import pytest
from sherd_connectors import registry, synth
from sherd_connectors.pipeline import run_import
from sherd_connectors.testing import export_path, load_meta, variants
from sherd_core import Store


@pytest.fixture(scope="session")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The `sherd demo` database: every connector fixture plus the synth demo profile."""
    path = tmp_path_factory.mktemp("demo") / "demo.duckdb"
    with Store.open(path) as store:
        for connector in registry.discover().values():
            for variant in variants(connector):
                run_import(store, connector, export_path(variant), load_meta(variant))
        synth.import_profile(store, "demo")
    return path


@pytest.fixture
def demo_store(demo_db: Path) -> Iterator[Store]:
    with Store.open(demo_db, read_only=True, sandboxed=True) as store:
        yield store


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty $SHERD_HOME with no API keys in the environment."""
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SHERD_AGENT_STUB"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path
