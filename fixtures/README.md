# Connector fixtures

Synthetic exports that connector golden tests, `sherd demo` and the docs run on.
**Never real data:** every value is invented, and phones, emails and IBANs use reserved values
only (`+1 555 0100`–`0199`, `example.com/.org/.net` or `.test`/`.invalid` domains, IBANs starting
with `XX`). `scripts/pii_scan.py` checks every tracked file.

## Layout

```
fixtures/<connector_id>/<variant>/
├── export/          # the synthetic export: a file or a folder, laid out like the real one
├── meta.json        # how to import it
└── expected.jsonl   # the canonical rows the connector must produce
```

`<connector_id>` is the connector's `id`; the connector's `fixtures()` returns its variant
directories. Each variant covers one shape of the export (an OS, a locale, an old format, …).

## `meta.json`

```json
{
  "tz": "Europe/Bucharest",
  "self_identities": ["Alex Demo", "+1 555 0100"],
  "description": "Android export, Romanian locale, one group chat",
  "export": "export/_chat.txt"
}
```

- `tz` (required): the IANA zone the export's local times are in. Fixtures never prompt.
- `self_identities`: the names or numbers that are "me" (`ImportContext.self_identities`).
- `description`: one line on what the variant covers.
- `export` (optional, default `"export"`): the path, relative to the variant directory, handed
  to `detect` and `parse`. Point it at a single file (`export/_chat.txt`, a `.zip`) to test a
  one-file export; leave it out when the export is the `export/` folder.

`ImportContext.export_root` is the export path when it is a folder, otherwise its parent;
`source_file` values are relative to it.

## `expected.jsonl`

One canonical row per line, as `sherd_connectors.testing.canonical_rows` produces it: the row's
`model_dump(mode="json")` plus `"table"`, without the store-set fields (`id`, `source`,
`import_id`, `imported_at`), keys sorted, rows sorted by (`table`, `source_row_id`). A repeated
(`table`, `source_row_id`) with identical canonical content keeps its first occurrence, as
the store does. Different content for the same identity raises `IdCollisionError`, naming
the table, source row id and differing fields without including the content. Both comparison
and update mode reject these collisions; updates replace the golden atomically only after
parsing and canonicalisation succeed.

Do not write it by hand. Create or refresh it with

```sh
SHERD_UPDATE_GOLDENS=1 uv run pytest packages/connectors/tests/test_goldens.py
```

then review the diff like code: the golden file *is* the connector's specification.

## Tests you get for free

`packages/connectors/tests/test_goldens.py` runs `assert_golden` on every registered connector ×
variant, so a connector WP only adds fixtures and its own connector-specific tests. For memory,
`sherd_connectors.testing.assert_streaming(connector, export_path, max_rss_mb)` parses an export
in a fresh process and checks its peak RSS; large exports for it come from the connector's own
`synth_<id>.py`, implementing `sherd_connectors.synth.raw.RawGenerator`.

## Synthetic canonical data

`sherd_connectors.synth` generates canonical rows directly (no export files), deterministically
from a seed:

| profile | rows | use |
| --- | --- | --- |
| `demo` | ≈ 50k | `sherd demo`, dig tests, screenshots |
| `bench` | ≈ 2M | performance tests |

`bench` multiplies every rate-driven stream (conversations, listening sessions, card payments,
transfers, browsing, shell sessions) by 40; monthly calendar items (salary, rent,
subscriptions, exchanges, fees) stay monthly. Try it with
`uv run python -m sherd_connectors.synth --profile demo --db /tmp/demo.duckdb`.
