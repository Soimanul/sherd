"""The sherd core package: canonical schema, row models, the store and the catalog."""

from importlib.metadata import version as _version

from sherd_core.catalog import Catalog, load_catalog
from sherd_core.ids import content_hash, path_hash, row_id
from sherd_core.models import Event, Location, MediaPlay, Message, Row, Transaction
from sherd_core.store import Store, UpsertStats

__version__ = _version("sherd-core")

__all__ = [
    "Catalog",
    "Event",
    "Location",
    "MediaPlay",
    "Message",
    "Row",
    "Store",
    "Transaction",
    "UpsertStats",
    "content_hash",
    "load_catalog",
    "path_hash",
    "row_id",
]
