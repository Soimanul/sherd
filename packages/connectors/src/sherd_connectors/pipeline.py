"""The import pipeline: one export, one import ledger entry (PLAN §4.4, §5).

Open exactly one writable `Store` per process: opening a second one marks the running imports
of the first as failed.
"""

from collections.abc import Iterable
from pathlib import Path

from sherd_core import Row, Store, UpsertStats, path_hash

from sherd_connectors.base import Connector, ImportContext


def import_rows(
    store: Store,
    connector: str,
    connector_version: str,
    hashed_path: str,
    tz: str,
    sources: Iterable[tuple[str, Iterable[Row]]],
) -> UpsertStats:
    """Record one import and upsert each `(source, rows)` stream into it.

    Returns the import's ledger. On any exception the import is marked failed (the batches
    already written stay) and the exception propagates.
    """
    import_id = store.begin_import(connector, connector_version, hashed_path, tz)
    try:
        for source, rows in sources:
            store.upsert(import_id, source, rows)
    except BaseException:
        store.finish_import(import_id, "failed")
        raise
    return store.finish_import(import_id, "succeeded")


def run_import(store: Store, connector: Connector, path: Path, ctx: ImportContext) -> UpsertStats:
    """Parse the export at `path` with `connector` and upsert its rows into `store`."""
    return import_rows(
        store,
        connector.id,
        connector.version,
        path_hash(path),
        ctx.tz.key,
        [(connector.id, connector.parse(path, ctx))],
    )
