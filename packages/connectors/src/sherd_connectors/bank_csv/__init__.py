"""Bank CSV connector, discovered through CONNECTOR."""

from collections.abc import Iterator
from pathlib import Path

from sherd_core import Transaction

from sherd_connectors.bank_csv.engine import choose, files, parse, streams
from sherd_connectors.bank_csv.mapping import load_mappings
from sherd_connectors.base import DetectResult, ImportContext


class BankCsvConnector:
    id = "bank_csv"
    version = "1"
    display_name = "Bank CSV"

    def detect(self, path: Path) -> DetectResult:
        mappings = load_mappings()
        checked = 0
        for file in files(path):
            with streams(file) as sources:
                for _, source in sources:
                    mapping = choose(source.read(65536), mappings)
                    if mapping:
                        return DetectResult(0.95, f"bank mapping: {mapping.id}")
                    checked += 1
                    if checked >= 3:
                        return DetectResult(0.0, "no matching bank headers")
        return DetectResult(0.0, "no matching bank headers")

    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Transaction]:
        yield from parse(path, ctx, load_mappings())

    def fixtures(self) -> list[Path]:
        root = Path(__file__).resolve().parents[5] / "fixtures" / self.id
        return sorted(
            p for p in root.glob("*") if (p / "meta.json").is_file() and p.name != "user-mapping"
        )


CONNECTOR = BankCsvConnector()
