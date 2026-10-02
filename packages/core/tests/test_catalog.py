from pathlib import Path

import pytest
from pydantic import ValidationError
from sherd_core import Store, load_catalog
from sherd_core.schema import SCHEMA_VERSION


def live_schema(store: Store) -> dict[str, list[str]]:
    rows = store.query(
        "SELECT table_name, column_name FROM duckdb_columns()"
        " WHERE schema_name = 'main' AND database_name = current_database()"
        " ORDER BY table_name, column_index"
    ).to_pylist()
    schema: dict[str, list[str]] = {}
    for row in rows:
        schema.setdefault(row["table_name"], []).append(row["column_name"])
    return schema


def test_catalog_describes_every_table_and_column(store: Store) -> None:
    catalog = load_catalog()
    schema = live_schema(store)
    assert len(schema) == 9
    assert set(catalog.tables) == set(schema)
    for table, columns in schema.items():
        doc = catalog.tables[table]
        assert doc.description.strip(), table
        assert list(doc.columns) == columns, table
        for column in columns:
            assert doc.columns[column].strip(), f"{table}.{column}"


def test_catalog_matches_schema_version() -> None:
    catalog = load_catalog()
    assert catalog.schema_version == SCHEMA_VERSION
    assert catalog.conventions
    assert set(catalog.common_columns) <= set(catalog.tables["messages"].columns)


def test_catalog_states_key_conventions() -> None:
    catalog = load_catalog()
    transactions = catalog.tables["transactions"].columns
    assert "negative" in transactions["amount"].lower()
    assert "utc" in catalog.tables["messages"].columns["ts"].lower()
    for value in ("direct", "group"):
        assert f"'{value}'" in catalog.tables["messages"].columns["chat_kind"]


def test_load_catalog_from_path(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(
        "schema_version: 1\ndescription: d\nconventions: [c]\ncommon_columns: {}\n"
        "tables:\n  t:\n    description: a table\n    columns: {x: a column}\n"
    )
    assert load_catalog(path).tables["t"].columns == {"x": "a column"}


def test_empty_descriptions_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(
        "schema_version: 1\ndescription: d\nconventions: []\ncommon_columns: {}\n"
        "tables:\n  t:\n    description: a table\n    columns: {x: '  '}\n"
    )
    with pytest.raises(ValidationError, match="columns"):
        load_catalog(path)
