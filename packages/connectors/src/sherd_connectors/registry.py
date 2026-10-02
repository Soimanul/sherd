"""Connector discovery (PLAN §3.3) and detection ranking (PLAN §5)."""

import importlib
import importlib.metadata
import logging
import pkgutil
from collections.abc import Mapping
from pathlib import Path

import sherd_connectors
from sherd_connectors.base import Connector, DetectResult

logger = logging.getLogger("sherd.connectors")

ENTRY_POINT_GROUP = "sherd.connectors"
_NOT_CONNECTORS = frozenset({"synth"})


class RegistryError(Exception):
    """The installed connectors cannot be registered."""


def _builtin() -> list[tuple[str, Connector]]:
    found: list[tuple[str, Connector]] = []
    prefix = sherd_connectors.__name__ + "."
    for info in pkgutil.iter_modules(sherd_connectors.__path__, prefix):
        if not info.ispkg or info.name.removeprefix(prefix) in _NOT_CONNECTORS:
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception as error:
            raise RegistryError(f"cannot load {info.name}: {type(error).__name__}") from error
        connector = getattr(module, "CONNECTOR", None)
        if connector is not None:
            found.append((info.name, connector))
    return found


def _plugins() -> list[tuple[str, Connector]]:
    found: list[tuple[str, Connector]] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        origin = f"entry point {entry_point.value}"
        try:
            found.append((origin, entry_point.load()))
        except Exception as error:
            raise RegistryError(f"cannot load {origin}: {type(error).__name__}") from error
    return found


def discover() -> dict[str, Connector]:
    """Every built-in connector subpackage exposing `CONNECTOR`, plus `sherd.connectors` plugins."""
    registered: dict[str, Connector] = {}
    origins: dict[str, str] = {}
    for origin, connector in [*_builtin(), *_plugins()]:
        connector_id = getattr(connector, "id", None)
        if not isinstance(connector_id, str) or not connector_id:
            raise RegistryError(f"{origin} does not provide a connector with an `id`")
        if connector_id in registered:
            raise RegistryError(
                f"connector id {connector_id!r} is registered twice:"
                f" by {origins[connector_id]} and by {origin}"
            )
        registered[connector_id] = connector
        origins[connector_id] = origin
    return dict(sorted(registered.items()))


def rank(
    path: Path, connectors: Mapping[str, Connector] | None = None
) -> list[tuple[Connector, DetectResult]]:
    """Run every connector's `detect` on `path`, best match first.

    A connector whose `detect` raises is logged without content and ranked at 0.
    """
    candidates = discover() if connectors is None else connectors
    ranked: list[tuple[Connector, DetectResult]] = []
    for connector_id, connector in candidates.items():
        try:
            result = connector.detect(path)
        except Exception as error:
            logger.warning(
                "detect failed: connector=%s error=%s", connector_id, type(error).__name__
            )
            result = DetectResult(0.0, f"detect failed ({type(error).__name__})")
        ranked.append((connector, result))
    ranked.sort(key=lambda item: (-item[1].confidence, item[0].id))
    return ranked
