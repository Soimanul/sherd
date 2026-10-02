"""Contract B: the connector interface (PLAN §5)."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sherd_core.models import Row


@dataclass(frozen=True)
class DetectResult:
    confidence: float  # 0..1
    reason: str  # short, no personal content


class RawSink(Protocol):
    """Writes a connector's raw records to `raw.<connector>_<table>` (optional in v1)."""

    def write(self, table: str, record: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class ImportContext:
    export_root: Path
    tz: ZoneInfo  # resolved before parse; connectors only read it
    self_identities: frozenset[str]  # names/numbers that are "me" (sets is_from_me)
    raw: RawSink | None = None


class Connector(Protocol):
    id: str
    version: str  # bump when output changes
    display_name: str

    def detect(self, path: Path) -> DetectResult:
        """Fast check whether `path` looks like this connector's export; no full parse."""
        ...

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]:
        """Stream canonical rows; never loads a whole file."""
        ...

    def fixtures(self) -> list[Path]:
        """Fixture variant directories (`fixtures/<id>/<variant>/`)."""
        ...


def export_root(path: Path) -> Path:
    """The root `source_file` paths are relative to: `path` itself, or a file's directory."""
    return path if path.is_dir() else path.parent
