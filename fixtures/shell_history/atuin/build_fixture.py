"""Rebuild the tiny, entirely synthetic atuin database beside this script."""

import sqlite3
from pathlib import Path


def build(path: Path) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE history (id TEXT, timestamp INTEGER, duration INTEGER, exit INTEGER, "
            "command TEXT, cwd TEXT, session TEXT, hostname TEXT, deleted_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "synthetic-a",
                    1704067200123456789,
                    1500000000,
                    0,
                    "git status",
                    "/home/demo/project",
                    "synthetic-session",
                    "synthetic-host",
                    None,
                ),
                (
                    "synthetic-b",
                    1704067201000000000,
                    2000000,
                    1,
                    "tool --token=fake-secret",
                    "/tmp/project",
                    "synthetic-session",
                    "synthetic-host",
                    None,
                ),
                (
                    "synthetic-deleted",
                    1704067202000000000,
                    0,
                    0,
                    "echo deleted",
                    "/tmp",
                    "synthetic-session",
                    "synthetic-host",
                    1704067203,
                ),
                (
                    "synthetic-malformed",
                    None,
                    0,
                    0,
                    "echo invalid",
                    "/tmp",
                    "synthetic-session",
                    "synthetic-host",
                    None,
                ),
            ],
        )


if __name__ == "__main__":
    build(Path(__file__).parent / "export" / "history.db")
