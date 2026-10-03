# sherd-wa

Rust WhatsApp export parsing through PyO3, packaged by maturin as an abi3 extension
for Python 3.12 and later. `uv sync --all-packages` builds the workspace package.
Stable Rust (minimum 1.88) is required for source builds.

The connector selects Rust when `sherd_wa` imports successfully. Set
`SHERD_WA=python` to force the reference parser or `SHERD_WA=rust` to require the
extension. A missing extension falls back to Python; a broken native import logs
one warning with the exception class before falling back. Detection remains the
existing fast Python sample probe. The native package is the `fast` extra of
`sherd-connectors`; `sherd-cli` installs that extra.

Rust owns file/chunk reading, universal newlines, replacement UTF-8 decoding,
invisible marks, prefix/date-order detection, wall-clock validation, continuation
joining, and message classification. Python owns timezone resolution (including
DST), chat metadata, sender identities, content hashes, and canonical Pydantic rows.
Both paths expose the same `message_records` primitive boundary in the connector.
Invalid dates emit a record with a null wall time, so Python preserves its original
skip-and-count behavior. Native failures become Python exceptions; panics are caught
at the exported functions and iterator methods.

`detect_date_order(source)` scans a path or binary reader without retaining history.
`RecordIterator(source, order, prefixes=False)` returns batches of at most 1,024
records and stops a batch after 1 MiB of original bodies. Each record is
`(wall_time_or_none, sender_or_none, original, kind, text_or_none, media_type_or_none,
is_group_notice)`, with wall time `(year, month, day, hour, minute, second)`.
`prefixes=True` streams individual timestamped lines for Python's chat metadata pass.
The 64 KiB buffered reader supports binary zip member streams through bounded
`read(size)` calls. Memory depends on the largest message, one bounded batch, and
the connector's current timestamp occurrence group, rather than export length.

## Verification

```sh
source ~/.cargo/env
uv sync --locked --all-packages
scripts/check
cargo test --locked --manifest-path crates/sherd-wa/Cargo.toml
cargo clippy --locked --manifest-path crates/sherd-wa/Cargo.toml -- -D warnings
cargo fmt --manifest-path crates/sherd-wa/Cargo.toml --check
uvx maturin build --locked --release --manifest-path crates/sherd-wa/Cargo.toml --interpreter "$(uv python find 3.12)" --out dist
```

The WhatsApp suite runs all existing behavioral tests under both parsers, all eight
canonical goldens under both parsers, and the 50 MiB `assert_streaming` check under
both parsers. Additional regressions cover replacement decoding in loose/zip inputs,
forced Python, a monkeypatched missing Rust import, chunk splits including emoji and
CRLF, Unicode timestamp digits, uppercase media markers, primitive parity, and FFI
errors. Rust unit tests cover date-order hints, malformed dates/clocks, Unicode
numbers, system/user distinctions, deletion and attachment classification.

`scripts/check` is unchanged: it runs Python formatting, lint, strict types, PII scan
and tests. The separate CI Rust job runs cargo tests, clippy with denied warnings,
format checking, and a release maturin wheel build, uploading the wheel artifact.
CI execution itself must be verified after pushing; this worker does not push.

Final local acceptance evidence (2026-10-03):

| Criterion | Evidence |
| --- | --- |
| 1. Golden parity and gate | 526 WhatsApp tests across both modes; all eight canonical JSONL exports also compared byte-for-byte to the goldens and between parsers in a manual check. |
| 2. Speed and memory | 1 GiB benchmark below: 18.60× primitive parsing, 3.37× complete connector; all counts equal and peak RSS below 70 MiB. |
| 3. Rust quality | 4 Rust unit tests passed; cargo clippy with `-D warnings` and cargo fmt check passed. Release abi3 wheel built and imported/parsed in a fresh isolated Python process. |
| 4. Fallback | `test_python_override_does_not_import_rust`, `test_missing_rust_module_falls_back`; full reference mode also passes the behavioral suite and goldens. |
| 5. Full gate and repository | `SHERD_BENCH=1 scripts/check`: 1,861 passed, no skips, 182.55 seconds; format, lint, strict types and tracked-file PII scan passed. One expected existing network-guard warning. Changes committed on `wp19-rust`, with clean git status verified before completion. |

The ordinary gate initially passed 1,860 tests and skipped the existing opt-in
2M-row cross-metrics benchmark. Enabling `SHERD_BENCH=1` in the final gate exercised
that test too. No required WP-19 check was skipped.

## Manual benchmark

```sh
uv run python crates/sherd-wa/benches/benchmark.py /tmp/sherd-wa-1gb.txt
```

This generates at least 1 GiB using the merged `WhatsAppGenerator` (en-US, seed 4)
from `synth_whatsapp.py`. Each measurement runs in a fresh Python process, consumes
rows without retaining them, and reports elapsed time, count, canonical-record
SHA-256, and peak RSS. The
`records` measurement includes date-order detection and text → primitive records
with timestamp parsing and classification on both sides; it excludes chat metadata,
timezones, hashes, and Pydantic construction. `connector` measures complete
`connector.parse` without database insertion. Equal counts and digests are required. Times are
manual evidence, not gate assertions.

Measured on 2026-10-03, Apple M5 / macOS arm64, Python 3.12.14, Rust 1.97.1,
release build, Europe/Bucharest. Input: 1,073,741,898 bytes, 7,044,110 records.

| Boundary | Python seconds / peak RSS MiB | Rust seconds / peak RSS MiB | Speedup |
| --- | --- | --- | --- |
| Date order + primitive records | 118.8803 / 69.05 | 6.3906 / 69.83 | **18.60×** |
| Complete connector.parse | 207.4996 / 69.30 | 61.5128 / 69.56 | **3.37×** |

All four measurements emitted 7,044,110 records. The primitive boundary exceeds
the required 10× speedup. Complete connector parsing still pays Python's chat
metadata, timezone, identity hashing, and Pydantic costs. Initial project checks
ran concurrently with part of the benchmark; these are observed wall times on
this host, rather than an isolated performance laboratory result.

## Known local macOS toolchain issue

On this worker's Xcode 27 linker, the built library was rejected by dyld with
`mis-aligned LINKEDIT string pool`. Rust's bundled LLVM linker with the installed
26.5 SDK produced a loadable extension. This is a local environment workaround,
not a repository linker override; Linux CI uses its normal stable Rust toolchain.
On an affected arm64 Mac with that SDK installed, build with:

```sh
source ~/.cargo/env
export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
export RUSTFLAGS="-C link-arg=-fuse-ld=$(rustc --print sysroot)/lib/rustlib/aarch64-apple-darwin/bin/gcc-ld/ld64.lld"
uv sync --reinstall-package sherd-wa --all-packages
```

The exports also apply to the cargo and maturin verification commands above.
Other machines should use their normal linker and SDK. Native parity tests require
a successful import explicitly, preventing silent fallback from masking a broken
extension build.
