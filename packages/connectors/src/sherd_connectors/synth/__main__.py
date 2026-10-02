"""`python -m sherd_connectors.synth --profile P --db PATH`: import a profile, print counts."""

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from sherd_core import Store

from sherd_connectors.synth.generator import PROFILES
from sherd_connectors.synth.importer import import_profile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sherd_connectors.synth", description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    start = time.perf_counter()
    with Store.open(args.db) as store:
        stats = import_profile(store, args.profile, args.seed)
        counts = store.table_counts()
    seconds = time.perf_counter() - start
    for table, count in counts.items():
        print(f"{table:<12} {count:>10,}")
    print(f"{stats.seen:,} rows seen, {stats.inserted:,} inserted in {seconds:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
