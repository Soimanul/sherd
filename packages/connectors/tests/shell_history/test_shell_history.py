import logging
import sqlite3
import tempfile
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest
from sherd_connectors.base import ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_connectors.shell_history import CONNECTOR, unmetafy
from sherd_connectors.shell_history.redaction import REDACTED, redact
from sherd_connectors.shell_history.synth_shell_history import ShellHistoryRaw
from sherd_connectors.testing import assert_streaming, export_path, load_meta
from sherd_core import Store

ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "shell_history"


def context(path: Path) -> ImportContext:
    return ImportContext(export_root(path), ZoneInfo("Europe/Bucharest"), frozenset())


@pytest.mark.parametrize(
    ("command", "expected", "count"),
    [
        ("tool --password=demo", f"tool --password={REDACTED}", 1),
        ("tool --token demo", f"tool --token {REDACTED}", 1),
        ('tool --secret "demo value"', f"tool --secret {REDACTED}", 1),
        ("tool --api-key='demo value'", f"tool --api-key={REDACTED}", 1),
        ("mysql -pdemo", f"mysql -p{REDACTED}", 1),
        ("export TOKEN=demo", f"export TOKEN={REDACTED}", 1),
        ('env TOKEN="one;two" tool', f"env TOKEN={REDACTED} tool", 1),
        ('tool --password="one\\"two"', f"tool --password={REDACTED}", 1),
        ("env SOME_SECRET=demo tool", f"env SOME_SECRET={REDACTED} tool", 1),
        ("export PASSWORD=one AUTH=two", f"export PASSWORD={REDACTED} AUTH={REDACTED}", 2),
        ("env passwd=demo APIKEY=demo tool", f"env passwd={REDACTED} APIKEY={REDACTED} tool", 2),
        ('curl -H "Authorization: Bearer demo"', f'curl -H "Authorization: Bearer {REDACTED}"', 1),
        ("curl https://demo:fake@example.com/a", f"curl https://{REDACTED}@example.com/a", 1),
        ("echo ghp_synthetic", f"echo {REDACTED}", 1),
        ("echo gho_synthetic", f"echo {REDACTED}", 1),
        ("echo github_pat_synthetic", f"echo {REDACTED}", 1),
        ("echo sk-abcdefghijklmnopqrst", f"echo {REDACTED}", 1),
        ("echo xoxb-synthetic-demo", f"echo {REDACTED}", 1),
        (
            "echo xoxa-synthetic xoxp-synthetic xoxr-synthetic",
            f"echo {REDACTED} {REDACTED} {REDACTED}",
            3,
        ),
        ("echo AKIAABCDEFGHIJKLMNOP", f"echo {REDACTED}", 1),
        ('git commit -m "token bucket"', 'git commit -m "token bucket"', 0),
        ("echo sk-short", "echo sk-short", 0),
        ("tool --token-count 3", "tool --token-count 3", 0),
        ("env PATH=/tmp tool", "env PATH=/tmp tool", 0),
        ("cp -pr a b", "cp -pr a b", 0),
        ("mkdir -pv x", "mkdir -pv x", 0),
        ("ls -pF", "ls -pF", 0),
        ("tar -pxf a", "tar -pxf a", 0),
        ("psql -p5432", "psql -p5432", 0),
        ("tool --password-file a", "tool --password-file a", 0),
        ("export TOKEN=$(cmd arg)", f"export TOKEN={REDACTED}", 1),
        ("curl https://demo:p@ss@example.com/a", f"curl https://{REDACTED}@example.com/a", 1),
        ("mysqldump -pdemo", f"mysqldump -p{REDACTED}", 1),
        ("mysqladmin -pdemo", f"mysqladmin -p{REDACTED}", 1),
        ("mariadb -pdemo", f"mariadb -p{REDACTED}", 1),
        ("git status -p", "git status -p", 0),
    ],
)
def test_redaction_table(command: str, expected: str, count: int) -> None:
    assert redact(command) == (expected, count)


def test_unmetafy() -> None:
    assert unmetafy(b"echo plain") == "echo plain"
    assert unmetafy(b"caf\x83\xe3\x83\x89") == "café"
    assert unmetafy(b"\xff\x83") == "��"


@pytest.mark.parametrize(
    ("variant", "filename", "confidence"),
    [
        ("zsh-extended", ".zsh_history", 1.0),
        ("bash-timestamped", ".bash_history", 0.95),
        ("atuin", "history.db", 0.95),
    ],
)
def test_detect(variant: str, filename: str, confidence: float) -> None:
    directory = ROOT / variant / "export"
    assert CONNECTOR.detect(directory).confidence == confidence
    assert CONNECTOR.detect(directory / filename).confidence == confidence


def test_bash_without_timestamps_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".bash_history"
    path.write_text("git status\necho synthetic\n")
    assert CONNECTOR.detect(path).confidence == 0
    assert list(CONNECTOR.parse(path, context(path))) == []


def test_other_exports_rejected(tmp_path: Path) -> None:
    for path in (ROOT.parent).glob("*/*/export"):
        if path.parent.parent != ROOT:
            assert CONNECTOR.detect(path).confidence == 0
    (tmp_path / "other.json").write_text('{"synthetic": true}')
    assert CONNECTOR.detect(tmp_path).confidence == 0
    assert CONNECTOR.detect(tmp_path / "missing").confidence == 0


def test_detect_read_budget(tmp_path: Path) -> None:
    path = tmp_path / ".bash_history"
    path.write_bytes(b"a" * 65536 + b"\n#1704067200\necho synthetic\n")
    assert CONNECTOR.detect(path).confidence == 0


def test_rows_multiline_duplicates_and_malformed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    variant = ROOT / "zsh-extended"
    rows = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert len(rows) == 4
    assert rows[0].source_row_id == rows[3].source_row_id
    assert rows[1].title == "echo one \\\n  two"
    assert rows[2].title == "echo café"
    assert rows[0].ts.isoformat() == "2024-01-01T00:00:00+00:00"
    assert all(row.url is None and row.kind == "shell.command" for row in rows)
    assert "malformed=2" in caplog.text
    assert "echo" not in caplog.text
    variant = ROOT / "bash-timestamped"
    bash = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert len(bash) == 3
    assert bash[1].title == 'for x in one two; do\n  echo "$x"\ndone'


def test_atuin_deleted_malformed_privacy_and_read_only(tmp_path: Path) -> None:
    path = ROOT / "atuin/export/history.db"
    before = path.read_bytes()
    rows = list(CONNECTOR.parse(path, context(path)))
    assert path.read_bytes() == before
    assert [row.source_row_id for row in rows] == ["synthetic-a", "synthetic-b"]
    assert rows[0].meta == {"shell": "atuin", "duration_ms": 1500, "exit": 0, "cwd": "~/project"}
    assert type(rows[0].meta["duration_ms"]) is int
    assert rows[0].ts.microsecond == 123456
    assert rows[1].title == f"tool --token={REDACTED}"
    # URI mode=ro must not create a missing database.
    with pytest.raises(sqlite3.OperationalError):
        list(CONNECTOR._atuin(tmp_path / "missing.db", "history.db"))
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize(
    "variant", ["zsh-extended", "bash-timestamped", "atuin", "mixed-dir", "secrets"]
)
def test_idempotency(variant: str, tmp_path: Path) -> None:
    fixture = ROOT / variant
    with Store.open(tmp_path / "life.duckdb") as store:
        first = run_import(store, CONNECTOR, export_path(fixture), load_meta(fixture))
        second = run_import(store, CONNECTOR, export_path(fixture), load_meta(fixture))
        assert first.inserted > 0
        assert second.inserted == 0
        assert first.seen == second.seen


@pytest.mark.parametrize("broken", [False, True])
def test_zip_streams_and_cleans_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    archive = tmp_path / "history.zip"
    with ZipFile(archive, "w") as output:
        for file in (ROOT / "mixed-dir/export").iterdir():
            if file.name == "history.db" and broken:
                output.writestr("nested/history.db", b"SQLite format 3\x00invalid")
            else:
                output.write(file, "nested/" + file.name)
    created: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    def temporary_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
        directory = real_temporary_directory(prefix=prefix, dir=tmp_path)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary_directory)
    assert CONNECTOR.detect(archive).confidence >= 0.95
    if broken:
        with pytest.raises(sqlite3.DatabaseError):
            list(CONNECTOR.parse(archive, context(archive)))
    else:
        rows = list(CONNECTOR.parse(archive, context(archive)))
        assert len(rows) == 9
        assert all(row.source_file.startswith("nested/") for row in rows)
    assert created
    assert all(not path.exists() for path in created)


def test_identity_independent_of_export_path(tmp_path: Path) -> None:
    source = ROOT / "zsh-extended/export/.zsh_history"
    destination = tmp_path / ".zsh_history"
    destination.write_bytes(source.read_bytes())
    assert [row.source_row_id for row in CONNECTOR.parse(source, context(source))] == [
        row.source_row_id for row in CONNECTOR.parse(destination, context(destination))
    ]


def test_streaming(tmp_path: Path) -> None:
    path = tmp_path / ".zsh_history"
    ShellHistoryRaw().write(path, 50 * 2**20, seed=8)
    assert path.stat().st_size >= 50 * 2**20
    with path.open("rb") as stream:
        assert all(len(stream.readline()) < 100 for _ in range(100))
    assert_streaming(CONNECTOR, path, max_rss_mb=200)


@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_huge_digits_skipped_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, shell: str
) -> None:
    caplog.set_level(logging.INFO)
    path = tmp_path / "renamed.history"
    huge = "9" * 5000
    text = (
        f": {huge}:0;echo bad\n: 1704067200:{huge};echo bad\n: 1704067200:0;echo good\n"
        if shell == "zsh"
        else f"#{huge}\necho bad\n#1704067200\necho good\n"
    )
    path.write_text(text)
    rows = list(CONNECTOR.parse(path, context(path)))
    assert [row.title for row in rows] == ["echo good"]
    assert "skipped lines=2" in caplog.text


def test_occurrences_reset_on_timestamp_change(tmp_path: Path) -> None:
    path = tmp_path / "renamed.history"
    path.write_text(
        ": 1704067200:0;echo a\n: 1704067200:0;echo a\n"
        ": 1704067201:0;echo b\n: 1704067200:0;echo a\n"
    )
    rows = list(CONNECTOR.parse(path, context(path)))
    assert rows[0].source_row_id != rows[1].source_row_id
    assert rows[0].source_row_id == rows[3].source_row_id


@pytest.mark.parametrize(
    "columns",
    ["other TEXT", "id TEXT, timestamp INTEGER", "id TEXT, timestamp INTEGER, command TEXT"],
)
def test_atuin_schema_detection_and_missing_columns(tmp_path: Path, columns: str) -> None:
    path = tmp_path / "renamed.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE history ({columns})")
        if "command" in columns:
            conn.execute(
                "INSERT INTO history VALUES (?, ?, ?)",
                ("synthetic-old", 1704067200000000000, "echo demo"),
            )
    supported = "command" in columns
    assert CONNECTOR.detect(path).confidence == (0.95 if supported else 0)
    rows = list(CONNECTOR.parse(path, context(path)))
    assert len(rows) == int(supported)
    if rows:
        assert rows[0].meta == {"shell": "atuin", "duration_ms": None, "exit": None, "cwd": None}


@pytest.mark.parametrize(
    ("variant", "filename"),
    [
        ("zsh-extended", ".zsh_history"),
        ("bash-timestamped", ".bash_history"),
        ("atuin", "history.db"),
    ],
)
def test_renamed_files_detected_by_content(tmp_path: Path, variant: str, filename: str) -> None:
    source = ROOT / variant / "export" / filename
    path = tmp_path / "backup.history"
    path.write_bytes(source.read_bytes())
    assert CONNECTOR.detect(path).confidence >= 0.95
    assert list(CONNECTOR.parse(path, context(path)))


@pytest.mark.parametrize("archived", [False, True])
def test_unrelated_sqlite_rejected(tmp_path: Path, archived: bool) -> None:
    database = tmp_path / "history.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE unrelated (id TEXT, timestamp INTEGER, command TEXT)")
    path = database
    if archived:
        path = tmp_path / "history.zip"
        with ZipFile(path, "w") as archive:
            archive.write(database, "history.db")
    assert CONNECTOR.detect(path).confidence == 0
    assert list(CONNECTOR.parse(path, context(path))) == []
