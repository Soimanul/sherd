"""Discover built-in modules and third-party digs without a central list."""

import importlib
import importlib.metadata
import pkgutil
from typing import cast

from sherd_core import Store

from sherd_insights import digs
from sherd_insights.base import Dig


class RegistryError(ValueError):
    """An installed dig cannot be registered."""


def discover() -> dict[str, Dig]:
    found: list[Dig] = []
    for info in pkgutil.iter_modules(digs.__path__, digs.__name__ + "."):
        module = importlib.import_module(info.name)
        found.extend(getattr(module, "DIGS", []))
    for entry in importlib.metadata.entry_points(group="sherd.digs"):
        found.append(cast(Dig, entry.load()))
    registered: dict[str, Dig] = {}
    for dig in found:
        if dig.id in registered:
            raise RegistryError(f"dig id {dig.id!r} is registered twice")
        registered[dig.id] = dig
    return dict(sorted(registered.items()))


def available(store: Store) -> list[Dig]:
    registered = discover()
    counts = store.table_counts()
    has_facts = any(counts.values())
    # table_counts covers fact tables; plugins may also require derived tables or the ledger.
    extra = {table for dig in registered.values() for table in dig.requires} - counts.keys()
    for table in sorted(extra):
        exists = store.query(
            "SELECT count(*) AS n FROM duckdb_tables() WHERE schema_name = 'main' "
            "AND database_name = current_database() AND table_name = ?",
            [table],
        ).to_pylist()[0]["n"]
        if exists:
            quoted = table.replace('"', '""')
            counts[table] = int(
                store.query(f'SELECT count(*) AS n FROM main."{quoted}"').to_pylist()[0]["n"]
            )
    return [
        dig
        for dig in registered.values()
        if (all(counts.get(t, 0) for t in dig.requires) if dig.requires else has_facts)
    ]
