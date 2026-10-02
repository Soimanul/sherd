"""Golden tests for every registered connector and fixture variant (PLAN §5).

Connector WPs add fixtures under `fixtures/<connector_id>/<variant>/`; they get a test here
without editing this file.
"""

from pathlib import Path

import pytest
from sherd_connectors.registry import discover
from sherd_connectors.testing import assert_golden, variants

CASES = [
    pytest.param(connector_id, variant, id=f"{connector_id}/{variant.name}")
    for connector_id, connector in discover().items()
    for variant in variants(connector)
]


@pytest.mark.parametrize(("connector_id", "variant"), CASES)
def test_golden(connector_id: str, variant: Path) -> None:
    assert_golden(discover()[connector_id], variant)


def test_every_connector_ships_fixtures() -> None:
    assert [cid for cid, connector in discover().items() if not variants(connector)] == []
