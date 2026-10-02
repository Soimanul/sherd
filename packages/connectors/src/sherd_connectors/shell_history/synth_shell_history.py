"""Deterministic large zsh exports for memory benchmarks."""

from pathlib import Path


class ShellHistoryRaw:
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        written = 0
        index = 0
        with path.open("wb") as output:
            while written < approx_bytes:
                line = f": {1704067200 + index}:1;echo fixture-{seed}-{index}\n".encode()
                output.write(line)
                written += len(line)
                index += 1
