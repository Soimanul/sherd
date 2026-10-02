"""Deterministic ids and hashes (PLAN §4.1, §4.3)."""

import hashlib
import os
from pathlib import Path

_SEP = "\x1f"


def _sha256_32(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def row_id(source: str, source_row_id: str) -> str:
    """Primary key of a fact row: sha256(source + "\\x1f" + source_row_id), hex, first 32."""
    return _sha256_32(source + _SEP + source_row_id)


def content_hash(*fields: object) -> str:
    """Stable hash of a record's identifying fields, for connectors building `source_row_id`.

    Fields are stringified (None becomes "") and joined with "\\x1f"; sha256 hex, first 32.
    """
    return _sha256_32(_SEP.join("" if field is None else str(field) for field in fields))


def path_hash(root: Path) -> str:
    """sha256 hex of the sorted (relative posix path, size) list of the files of an export.

    `root` may be a directory (every file under it, symlinked directories not followed) or a
    single file (its name and size).
    """
    if root.is_file():
        entries = [(root.name, root.stat().st_size)]
    elif root.is_dir():
        entries = []
        for directory, _, files in os.walk(root):
            for name in files:
                path = Path(directory, name)
                if path.is_file():
                    entries.append((path.relative_to(root).as_posix(), path.stat().st_size))
    else:
        raise FileNotFoundError(f"no export at {root}")
    digest = hashlib.sha256()
    for relative, size in sorted(entries):
        digest.update(f"{relative}{_SEP}{size}\x1e".encode())
    return digest.hexdigest()
