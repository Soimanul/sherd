"""Deterministic synthetic Chrome exports for streaming benchmarks."""

import json
from pathlib import Path


class GoogleTakeoutGenerator:
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        with path.open("w", encoding="utf-8") as out:
            out.write('{"Browser History":[')
            written = 20
            index = 0
            while written < approx_bytes:
                record = json.dumps(
                    {
                        "title": f"Synthetic {index}",
                        "url": f"https://example.com/{index}",
                        "time_usec": 1704067200000000 + seed + index,
                    },
                    separators=(",", ":"),
                )
                out.write(("," if index else "") + record)
                written += len(record) + 1
                index += 1
            out.write("]}")


GENERATOR = GoogleTakeoutGenerator()
