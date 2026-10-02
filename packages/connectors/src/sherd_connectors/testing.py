"""The golden-test harness for connectors (PLAN §5, fixtures/README.md).

A fixture variant is a directory `fixtures/<connector_id>/<variant>/` holding the synthetic
export (`export`, or the path named by `meta.json`'s optional `"export"` key), `meta.json`
and `expected.jsonl`.
"""

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sherd_connectors.base import Connector, ImportContext, export_root
from sherd_connectors.registry import discover

UPDATE_ENV = "SHERD_UPDATE_GOLDENS"
STORE_FIELDS = frozenset({"id", "source", "import_id", "imported_at"})
_DIFF_LIMIT = 5

CanonicalRow = dict[str, Any]


def variants(connector: Connector) -> list[Path]:
    """The connector's fixture variant directories, sorted."""
    return sorted(connector.fixtures())


def _meta(variant: Path) -> dict[str, Any]:
    meta = json.loads((variant / "meta.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"{variant / 'meta.json'} must hold a JSON object")
    return meta


def export_path(variant: Path) -> Path:
    """The export inside a variant: `meta.json`'s `"export"` (relative), default `export`."""
    relative = _meta(variant).get("export", "export")
    return variant / str(relative)


def load_meta(variant: Path) -> ImportContext:
    """The import context a variant declares in `meta.json` (`tz`, `self_identities`)."""
    meta = _meta(variant)
    return ImportContext(
        export_root=export_root(export_path(variant)),
        tz=ZoneInfo(str(meta["tz"])),
        self_identities=frozenset(str(value) for value in meta.get("self_identities", [])),
    )


def _key(row: CanonicalRow) -> tuple[str, str]:
    return (str(row["table"]), str(row["source_row_id"]))


def canonical_rows(connector: Connector, variant: Path) -> list[CanonicalRow]:
    """Parse a variant into JSON-ready rows as the store would keep them.

    Each row gains `"table"`; store-set fields are dropped; a repeated (table, source_row_id)
    keeps its first occurrence, like the store; rows are sorted by (table, source_row_id).
    """
    rows: dict[tuple[str, str], CanonicalRow] = {}
    for row in connector.parse(export_path(variant), load_meta(variant)):
        record = {k: v for k, v in row.model_dump(mode="json").items() if k not in STORE_FIELDS}
        record["table"] = row.table_name
        rows.setdefault(_key(record), record)
    return [rows[key] for key in sorted(rows)]


def _dumps(row: CanonicalRow) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def write_golden(connector: Connector, variant: Path) -> Path:
    """(Re)write `variant/expected.jsonl` from the connector's current output."""
    target = variant / "expected.jsonl"
    with target.open("w", encoding="utf-8", newline="\n") as out:
        for row in canonical_rows(connector, variant):
            out.write(_dumps(row) + "\n")
    return target


def _read_expected(path: Path) -> list[CanonicalRow]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def _diff(expected: Sequence[CanonicalRow], actual: Sequence[CanonicalRow]) -> list[str]:
    want = {_key(row): row for row in expected}
    got = {_key(row): row for row in actual}
    lines: list[str] = []
    missing = [key for key in want if key not in got]
    extra = [key for key in got if key not in want]
    changed = [key for key in want if key in got and want[key] != got[key]]
    for label, keys in (("missing", missing), ("unexpected", extra)):
        for table, row_id in keys[:_DIFF_LIMIT]:
            lines.append(f"  {label} row {table}/{row_id}")
        if len(keys) > _DIFF_LIMIT:
            lines.append(f"  … and {len(keys) - _DIFF_LIMIT} more {label} rows")
    for key in changed[:_DIFF_LIMIT]:
        before, after = want[key], got[key]
        for field in sorted(before.keys() | after.keys()):
            if before.get(field) != after.get(field):
                lines.append(
                    f"  {key[0]}/{key[1]} {field}: expected {before.get(field)!r},"
                    f" got {after.get(field)!r}"
                )
    if len(changed) > _DIFF_LIMIT:
        lines.append(f"  … and {len(changed) - _DIFF_LIMIT} more changed rows")
    if not lines and list(expected) != list(actual):
        lines.append("  same rows in a different order (expected.jsonl must be sorted)")
    return lines


def assert_golden(connector: Connector, variant: Path) -> None:
    """Compare the parsed variant to `expected.jsonl`; `SHERD_UPDATE_GOLDENS=1` rewrites it."""
    golden = variant / "expected.jsonl"
    if os.environ.get(UPDATE_ENV) == "1":
        write_golden(connector, variant)
        return
    if not golden.is_file():
        raise AssertionError(
            f"{golden} is missing; run the tests with {UPDATE_ENV}=1 to create it, then review it"
        )
    actual = canonical_rows(connector, variant)
    expected = _read_expected(golden)
    problems = _diff(expected, actual)
    if problems:
        raise AssertionError(
            f"{connector.id} output differs from {golden}"
            f" ({len(expected)} expected rows, {len(actual)} parsed):\n"
            + "\n".join(problems)
            + f"\nIf the change is intended, rerun with {UPDATE_ENV}=1 and review the diff."
        )


# -- streaming -------------------------------------------------------------------------------

_CHILD = """
import importlib, importlib.util, json, resource, sys
from pathlib import Path
from zoneinfo import ZoneInfo
from sherd_connectors.base import ImportContext, export_root

kind, *ref = json.loads(sys.argv[1])
if kind == "registry":
    from sherd_connectors.registry import discover
    connector = discover()[ref[0]]
else:
    file, module_name, attr, call = ref
    spec = importlib.util.spec_from_file_location(module_name, file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    connector = getattr(module, attr)
    if call:
        connector = connector()
path = Path(sys.argv[2])
ctx = ImportContext(export_root(path), ZoneInfo(sys.argv[3]), frozenset())
rows = sum(1 for _ in connector.parse(path, ctx))
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
peak_mb = peak / 2**20 if sys.platform == "darwin" else peak / 2**10
print(json.dumps({"rows": rows, "peak_rss_mb": peak_mb}))
"""


def _reference(connector: Connector) -> list[object]:
    """How a child process can rebuild `connector`."""
    registered = discover().get(connector.id)
    if registered is not None and type(registered) is type(connector):
        return ["registry", connector.id]
    module = sys.modules[type(connector).__module__]
    file = getattr(module, "__file__", None)
    if file is None:
        raise ValueError(f"cannot locate the module defining {type(connector).__qualname__}")
    for name, value in vars(module).items():
        if value is connector:
            return ["module", file, module.__name__, name, False]
    if "." in type(connector).__qualname__:
        raise ValueError("define the connector class at module level")
    return ["module", file, module.__name__, type(connector).__name__, True]


def measure_streaming(
    connector: Connector, export_path: Path, *, tz: str = "UTC"
) -> dict[str, Any]:
    """Parse `export_path` in a fresh process; return its row count and peak RSS in MB."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, json.dumps(_reference(connector)), str(export_path), tz],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        tail = completed.stderr.strip().splitlines()[-1:] or ["no output"]
        raise AssertionError(f"{connector.id} parse failed in the child process: {tail[0]}")
    result: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def assert_streaming(
    connector: Connector, export_path: Path, max_rss_mb: float, *, tz: str = "UTC"
) -> None:
    """Assert that parsing `export_path` in a fresh process peaks below `max_rss_mb`."""
    result = measure_streaming(connector, export_path, tz=tz)
    peak = float(result["peak_rss_mb"])
    if peak > max_rss_mb:
        raise AssertionError(
            f"{connector.id} peaked at {peak:.0f} MB RSS parsing {result['rows']} rows"
            f" (limit {max_rss_mb:.0f} MB); parse must stream"
        )
