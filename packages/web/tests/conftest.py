from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sherd_connectors import synth
from sherd_core import Store
from sherd_web.app import create_app

ClientFactory = Callable[..., TestClient]


@pytest.fixture(scope="session")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic `demo` profile, built once per session."""
    path = tmp_path_factory.mktemp("demo") / "demo.duckdb"
    with Store.open(path) as store:
        synth.import_profile(store, "demo")
    return path


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """A migrated database with no rows."""
    path = tmp_path / "empty.duckdb"
    Store.open(path).close()
    return path


@pytest.fixture
def client() -> ClientFactory:
    def make(db: Path, *, demo: bool = False, raise_server_exceptions: bool = True) -> TestClient:
        return TestClient(
            create_app(db, demo=demo),
            base_url="http://127.0.0.1:8765",
            raise_server_exceptions=raise_server_exceptions,
        )

    return make
