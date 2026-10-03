"""Manual benchmark: fresh processes, same primitive boundary, no retained rows.

Run: uv run python crates/sherd-wa/benches/benchmark.py /tmp/sherd-wa-1gb.txt
"""

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from sherd_connectors.base import ImportContext
from sherd_connectors.whatsapp import CONNECTOR
from sherd_connectors.whatsapp.parser import (
    ChatFile,
    file_date_order,
    message_records,
    parser_backend,
)
from sherd_connectors.whatsapp.synth_whatsapp import WhatsAppGenerator


def measure(path: Path, backend: str, layer: str) -> None:
    os.environ["SHERD_WA"] = backend
    native = parser_backend()
    if backend == "rust" and native is None:
        raise RuntimeError("Rust import failed; refusing to benchmark the fallback")
    start = perf_counter()
    digest = hashlib.sha256()
    rows = 0
    if layer == "records":
        file = ChatFile(path)
        order = file_date_order(file, native)
        for record in message_records(file, order, native):
            digest.update(
                json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            )
            digest.update(b"\n")
            rows += 1
    else:
        ctx = ImportContext(path.parent, ZoneInfo("Europe/Bucharest"), frozenset({"Alex Demo"}))
        for message in CONNECTOR.parse(path, ctx):
            digest.update(
                json.dumps(
                    message.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            digest.update(b"\n")
            rows += 1
    elapsed = perf_counter() - start
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024**2 if sys.platform == "darwin" else 1024)
    print(
        json.dumps(
            dict(
                backend=backend,
                layer=layer,
                rows=rows,
                sha256=digest.hexdigest(),
                seconds=elapsed,
                rss_mb=rss_mb,
            )
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--bytes", type=int, default=1024**3)
    parser.add_argument("--backend", choices=["python", "rust"])
    parser.add_argument("--layer", choices=["records", "connector"])
    args = parser.parse_args()
    if args.backend:
        measure(args.path, args.backend, args.layer)
        return
    if not args.path.exists() or args.path.stat().st_size < args.bytes:
        WhatsAppGenerator().write(args.path, args.bytes, seed=4)
    print(
        json.dumps(dict(bytes=args.path.stat().st_size, python=sys.version, platform=sys.platform)),
        flush=True,
    )
    results = {}
    for layer in ("records", "connector"):
        for backend in ("python", "rust"):
            result = subprocess.run(
                [sys.executable, __file__, str(args.path), "--backend", backend, "--layer", layer],
                check=True,
                capture_output=True,
                text=True,
            )
            print(result.stdout.strip(), flush=True)
            results[layer, backend] = json.loads(result.stdout)
        py, rs = results[layer, "python"], results[layer, "rust"]
        if py["rows"] != rs["rows"]:
            raise ValueError(f"{layer} row counts differ between backends")
        if py["sha256"] != rs["sha256"]:
            raise ValueError(f"{layer} digests differ between backends")
        print(json.dumps(dict(layer=layer, speedup=py["seconds"] / rs["seconds"])), flush=True)


if __name__ == "__main__":
    main()
