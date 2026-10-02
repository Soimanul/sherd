import hashlib
from pathlib import Path

import pytest
from sherd_cli.main import create_app
from sherd_connectors import synth
from sherd_core import Store
from sherd_insights import wrapped
from typer.testing import CliRunner, Result


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("wrapped") / "demo.duckdb"
    with Store.open(path) as store:
        synth.import_profile(store, "demo")
    return path


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TZ", "UTC")
    return tmp_path


def run(*args: str) -> Result:
    return CliRunner().invoke(create_app(), ["wrapped", *args])


def test_writes_six_cards_for_the_last_full_year(demo_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "cards"
    got = run("--db", str(demo_db), "--out", str(out))
    assert got.exit_code == 0, got.output
    expected = [out / f"wrapped-2025-{n}.png" for n in range(1, 7)]
    assert got.stdout.split() == [str(path) for path in expected]
    for path in expected:
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_same_input_gives_the_same_files(demo_db: Path, tmp_path: Path) -> None:
    def digests(out: Path) -> list[str]:
        got = run("2025", "--db", str(demo_db), "--out", str(out), "--tz", "Europe/Bucharest")
        assert got.exit_code == 0, got.output
        return [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in got.stdout.split()]

    assert digests(tmp_path / "a") == digests(tmp_path / "b")


def test_flags_reach_the_generator(
    demo_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, wrapped.WrappedOptions]] = []

    def fake(store: Store, year: int, options: wrapped.WrappedOptions) -> list[object]:
        seen.append((year, options))
        return []

    monkeypatch.setattr(wrapped, "render", fake)
    run("2024", "--db", str(demo_db), "--names", "--amounts", "--tz", "Europe/Bucharest")
    run("2024", "--db", str(demo_db))
    assert seen == [
        (2024, wrapped.WrappedOptions(names=True, amounts=True, tz="Europe/Bucharest")),
        (2024, wrapped.WrappedOptions(names=False, amounts=False, tz="UTC")),
    ]


def test_demo_flag_reads_the_demo_database(demo_db: Path, home: Path) -> None:
    (home / "home").mkdir()
    (home / "home" / "demo.duckdb").write_bytes(demo_db.read_bytes())
    got = run("--demo", "--out", str(home / "out"))
    assert got.exit_code == 0, got.output
    assert len(got.stdout.split()) == 6


def test_helpful_errors(demo_db: Path, tmp_path: Path) -> None:
    missing = run("--db", str(tmp_path / "nope.duckdb"))
    assert missing.exit_code == 1
    assert "No database yet" in missing.output
    empty_year = run("1999", "--db", str(demo_db), "--out", str(tmp_path / "x"))
    assert empty_year.exit_code == 1
    assert "nothing to wrap for 1999" in empty_year.output
    assert not (tmp_path / "x").exists()
    both = run("--db", str(demo_db), "--demo")
    assert both.exit_code == 1
    assert "not both" in both.output
    zone = run("--db", str(demo_db), "--tz", "Mars/Olympus")
    assert zone.exit_code == 1
    empty = tmp_path / "empty.duckdb"
    Store.open(empty).close()
    nothing = run("--db", str(empty))
    assert nothing.exit_code == 1
    assert "full calendar year" in nothing.output
