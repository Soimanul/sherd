from pathlib import Path

import pytest
from sherd_core.paths import default_db_path, sherd_home


def test_sherd_home_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path / "data"))
    assert sherd_home() == tmp_path / "data"
    assert default_db_path() == tmp_path / "data" / "life.duckdb"


@pytest.mark.parametrize("value", [None, ""])
def test_sherd_home_defaults_to_dot_sherd(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("SHERD_HOME", raising=False)
    else:
        monkeypatch.setenv("SHERD_HOME", value)
    assert sherd_home() == tmp_path / ".sherd"
    assert default_db_path() == tmp_path / ".sherd" / "life.duckdb"
