"""Deterministic raw Revolut exports for memory benchmarks."""

import csv
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = [
    "Type",
    "Product",
    "Started Date",
    "Completed Date",
    "Description",
    "Amount",
    "Fee",
    "Currency",
    "State",
    "Balance",
]


class BankCsvGenerator:
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(HEADERS)
            i = 0
            # Short records and distinct timestamps exercise occurrence-counter memory.
            description = "Fictional Market"
            while out.tell() < approx_bytes:
                stamp = (datetime(2024, 1, 1) + timedelta(seconds=i + seed)).isoformat(sep=" ")
                writer.writerow(
                    [
                        "CARD_PAYMENT",
                        "Current",
                        stamp,
                        stamp,
                        description,
                        "-12.50",
                        "0",
                        "RON",
                        "COMPLETED",
                        "100",
                    ]
                )
                i += 1


GENERATOR = BankCsvGenerator()
