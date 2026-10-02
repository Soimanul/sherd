"""Raw export generators for streaming and parser benchmarks.

Each connector implements one in its own `synth_<id>.py`; the framework only fixes the shape.
"""

from pathlib import Path
from typing import Protocol


class RawGenerator(Protocol):
    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        """Write a synthetic export of roughly `approx_bytes` to `path`, deterministically."""
        ...
