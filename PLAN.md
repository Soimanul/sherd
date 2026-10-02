# sherd — Implementation & Handoff Plan

*Version 1.1 · 2 October 2026 · Coordinator: Claude (Opus) in Orca; engineers routed by model tier (§10.4); Vlad is product owner.*

> **sherd** (n.): a fragment of pottery found at a dig, reassembled to reconstruct the whole vessel. **sherd** (software): a local-first tool that reassembles the data exports you already own into a private database, then lets you ask, explore, and see your own life.

**Changes in 1.1:** contracts A–D are now complete (every column, id rule and interface is written out); file ownership no longer has shared files between parallel WPs (§3.3); branch model `dev` → `main` (§15); model routing by tier (§10.4); offline means loopback-only; timezone lives in `ImportContext`; the result type is `pyarrow.Table`; a synthetic data generator is added to WP-03; PII rules use reserved values; WP-14 is split into 14a/14b.

---

## 0. How to use this document

This plan is written for an orchestrator, not a single developer. Conventions:

- **WP-xx** = a work package: one worktree, one engineer, one reviewer, one merge into `dev`. Every WP lists the files it owns, its inputs (contracts it depends on), its outputs (contracts it must satisfy), and acceptance criteria. Engineers must not edit files outside their ownership list; if they need to, they stop and ask the coordinator.
- **Contracts** are frozen interfaces defined in §4–§7. Changing one is its own small WP (a contract-change WP) merged before the WPs that need it.
- **Decisions** are marked **\[DECIDED\]** (do not reopen) or **\[OPEN\]** (coordinator + Vlad decide before the relevant WP starts). §12 lists all open decisions.

Roles:

- **Coordinator (Claude Opus, in Orca):** owns this document, the contracts, the task board, sequencing, model routing (§10.4) and merges into `dev`. Writes no product code beyond docs and contracts.
- **Engineers:** implement one WP each in their own Orca worktree branched from `dev`, with tests and fixtures. Model chosen per WP by tier (§10.4).
- **Reviewers:** always cross-model (a Codex build gets a Claude reviewer and vice versa). Review against the WP's acceptance criteria and the §11 checklist; they send work back rather than fixing it.
- **Design (Claude):** owns the design system, UI copy and web UI components (WP-14a/14b/15), fed by §9.
- **Vlad:** product owner, final say on open decisions, merges `dev` → `main`, tester with real personal data (never committed).

---

## 1. Product definition

### 1.1 The three verbs

| Verb | Command | What it does |
| --- | --- | --- |
| **Dig** | `sherd dig <path>` | Detects and imports any supported export into a local DuckDB file. Idempotent; re-running never duplicates. |
| **Ask** | `sherd ask "..."` | Natural-language question → read-only SQL → answer + chart. Always shows the SQL. Local model or BYO key. |
| **Show** | `sherd show` / `sherd web` | Pre-built **insights**: dashboards, trends, aggregations, comparisons, and the shareable *Wrapped*. No LLM required. |

**Show is not a bonus; it is the reason most people will install.** Most users will never type a question. They will open the dashboard, see their life in charts, and share one image. Ask is for the curious; Show is for everyone.

### 1.2 Value proposition

*Point sherd at the exports you already own. In five minutes you see your life in charts; you can ask it anything; and nothing leaves your machine unless you say so.*

### 1.3 Non-goals for v1 **\[DECIDED\]**

- No screen/audio recording. No live API polling of services (that's v2).
- No cloud, no accounts, no telemetry, no sync.
- The agent never writes data or takes actions (no sending messages, no payments).
- No mobile app. The Telegram bot is the "mobile" story.

---

## 2. What we learned from research (drives the plan)

1. **Naming:** `sherd` on PyPI is taken (a pottery-profile vectoriser, June 2026), and a small Crystal package manager on GitHub uses the name. The CLI command `sherd` is still free. **\[DECIDED\]** GitHub org `sherd-dev`, PyPI package `sherd-cli` (exposes the `sherd` command), crate `sherd-wa` for the Rust parser, domain candidates `sherd.dev` / `getsherd.app` (check).
2. **Prior art to reuse, not rewrite:** karlicoss/HPI has mature parsers (e.g. `google_takeout_parser`, browser-history exporters). Dogsheep has export→SQLite converters. Licence-check and vendor/depend where useful; credit prominently. Needs the licence decision (§12.1) first.
3. **Google location history is a mess:** three formats (Records.json, Semantic Location History monthly files, the newer phone export with `semanticSegments`), and many people have fragmented exports after Google's 2024 migration. → Location is **Phase 2** with its own WP.
4. **WhatsApp is the emotional core** of the demo, and its export format varies by platform (iOS vs Android), locale and date format, with multi-line messages, media placeholders and invisible Unicode marks. → It gets the Rust parser and the largest fixture set.
5. **Orca workflow:** one agent per git worktree, with diff comparison and merge. The WP structure maps 1:1 onto Orca tasks; Orca's Design Mode can push real UI elements into Claude's prompt for web UI work.

---

## 3. Architecture

```
                 ┌──────────────── connectors (plugins) ────────────────┐
 exports/  ───►  │ whatsapp · spotify · google-takeout · bank-csv · ... │
                 └───────────────┬──────────────────────────────────────┘
                                 │  normalized rows (canonical schema)
                                 ▼
                     ┌───────── sherd core ─────────────┐
                     │ DuckDB file  ~/.sherd/life.duckdb │
                     │ canonical tables (+ optional raw) │
                     │ semantic layer (catalog.yaml)     │
                     │ entity resolution (contacts)      │
                     └──────┬─────────────┬─────────────┘
                            │             │
               ┌────────────▼──┐     ┌────▼─────────────┐
               │  insights     │     │  agent (ask)     │
               │ (digs: SQL +  │     │ NL → SQL → chart │
               │  chart specs) │     │ read-only, shows │
               └──────┬────────┘     │ SQL, BYO/local   │
                      │              └────┬─────────────┘
        ┌─────────────┼───────────────────┼──────────────────┐
        ▼             ▼                   ▼                  ▼
      CLI/TUI     local web UI        MCP server         Telegram bot
                (dashboards,       (other agents can     (Phase 2)
                 Wrapped, share)    query your sherd)
```

### 3.1 Stack **\[DECIDED\]**

- **Python 3.12**, `uv` workspace for everything, `ruff` (format + lint) + `mypy --strict`, `pytest`.
- **DuckDB** as the store (single file, fast aggregations, reads CSV/JSON natively).
- **pyarrow** as the tabular result type (`pyarrow.Table`): DuckDB returns it natively with no copy. No pandas/polars dependency in core.
- **Pydantic v2** models for the canonical rows; connectors emit Pydantic rows.
- **Typer** CLI, **Rich** output, **Textual** for the TUI.
- **FastAPI** for the local web server; server-rendered **Jinja + HTMX + Alpine.js**; charts with **Vega-Lite** (the same spec renders in the web UI and exports to PNG via `vl-convert` for Wrapped). JS libraries are vendored into `static/vendor/` (no CDN, no Node build step).
- **Rust** crate `sherd-wa` (WhatsApp parser) via **PyO3/maturin**. The Python parser comes first; Rust replaces it behind the same interface.
- **LLM layer:** provider-agnostic client with adapters for Anthropic, OpenAI and **Ollama** (local). Prompt templates are versioned files.
- **MCP server** (stdio) exposing `list_tables`, `describe`, `query` (read-only), `insights`.

### 3.2 Repository layout **\[DECIDED\]**

`src` layout per package; the import name is `sherd_<package>`.

```
sherd/
├── pyproject.toml              # uv workspace root: tool config only (ruff, mypy, pytest)
├── uv.lock                     # committed; regenerated by the coordinator on merge conflicts
├── PLAN.md  AGENTS.md
├── scripts/check               # the one gate: CI and every engineer run exactly this
├── scripts/pii_scan.py         # PII pattern scan (pre-commit + CI), §11.3
├── packages/
│   ├── core/       src/sherd_core/        schema.py, models.py, store.py, catalog.py, entities.py
│   ├── connectors/ src/sherd_connectors/  base.py, registry.py, testing.py, synth/, whatsapp/, spotify/, ...
│   ├── insights/   src/sherd_insights/    base.py, registry.py, charts.py, theme/, digs/*.py, wrapped.py
│   ├── agent/      src/sherd_agent/       llm.py, providers/, sql_guard.py, prompts/, ask.py
│   ├── cli/        src/sherd_cli/         main.py, commands/*.py, tui/
│   ├── web/        src/sherd_web/         app.py, routes/, templates/, static/
│   └── mcp/        src/sherd_mcp/         server.py
│   (each package: pyproject.toml, src/<pkg>/py.typed, tests/)
├── crates/sherd-wa/            # Rust WhatsApp parser (PyO3)
├── fixtures/<connector_id>/<variant>/   # synthetic exports (NEVER real data) + expected.jsonl + meta.json
├── docs/                       # mkdocs
└── .github/workflows/          # ci.yml, release.yml
```

Distribution names: `sherd-core`, `sherd-connectors`, `sherd-insights`, `sherd-agent`, `sherd-web`, `sherd-mcp`, and `sherd-cli` (the installable app that depends on the others and exposes `sherd`).

### 3.3 Ownership rules (no shared files between parallel WPs) **\[DECIDED\]**

- **Built-in plugins are discovered, not listed.** Connectors: every subpackage of `sherd_connectors` that exposes `CONNECTOR` is registered by `sherd_connectors.registry`; third-party connectors register through the entry-point group `sherd.connectors`. Digs: every module in `sherd_insights.digs` that exposes `DIGS: list[Dig]` is registered; third-party digs use `sherd.digs`. CLI commands: every module in `sherd_cli.commands` that exposes `register(app: typer.Typer) -> None` is registered by `sherd_cli.main`. No WP edits a central list.
- **Fixtures** live in `fixtures/<connector_id>/`, owned by that connector's WP. Tests live in `packages/<pkg>/tests/<area>/`.
- **The insights index** is generated from the dig registry (`sherd show --list`, and a docs page built from it), never hand-edited.
- **Dependencies:** after WP-01, a package's `pyproject.toml` belongs to the WP that owns that package. When several parallel WPs share one package (connectors, digs), each may only **append** its own dependency lines; the coordinator resolves the trivial conflicts and regenerates `uv.lock` at merge.

---

## 4. Contract A — Canonical data model **\[DECIDED, v1\]**

### 4.1 Common columns

Every **fact table** (`messages`, `media_plays`, `transactions`, `events`, `locations`) starts with:

| column | type | rule |
| --- | --- | --- |
| `id` | VARCHAR PRIMARY KEY | `sha256(source + "\x1f" + source_row_id)`, hex, first 32 chars. Computed by the store. |
| `source` | VARCHAR NOT NULL | connector id, e.g. `whatsapp` |
| `source_file` | VARCHAR NOT NULL | path relative to the export root, `/`-separated |
| `source_row_id` | VARCHAR NOT NULL | stable key chosen by the connector (§4.3) |
| `import_id` | VARCHAR NOT NULL | → `imports.id` of the import that first inserted the row |
| `imported_at` | TIMESTAMPTZ NOT NULL | set by the store |
| `meta` | JSON NULL | connector extras that have no column; never required by a dig |

`UNIQUE (source, source_row_id)`. Insert semantics: **first write wins** (`ON CONFLICT DO NOTHING`). Re-importing the same export is a no-op; a newer export adds only new rows.

Enumerated columns are `VARCHAR` with a `CHECK (... IN (...))` constraint (not DuckDB `ENUM`, which is awkward to migrate).

### 4.2 Tables

```sql
messages(
  <common>,
  chat_id      VARCHAR NOT NULL,      -- connector-stable chat key
  chat_name    VARCHAR,
  chat_kind    VARCHAR NOT NULL CHECK (chat_kind IN ('direct','group')),
  sender_id    VARCHAR,               -- connector-local identity, e.g. 'whatsapp:+15550100' or 'whatsapp:name:Ana Pop'; NULL for system
  sender_name  VARCHAR,
  is_from_me   BOOLEAN NOT NULL,
  contact_id   VARCHAR,               -- → contacts.id, filled by entity resolution (WP-12); NULL until then
  ts           TIMESTAMPTZ NOT NULL,
  text         VARCHAR,               -- NULL for media without caption
  kind         VARCHAR NOT NULL CHECK (kind IN ('text','media','system','deleted')),
  media_type   VARCHAR CHECK (media_type IN ('image','video','audio','document','sticker','gif','other')),
  reply_to_id  VARCHAR                -- messages.id, when the export has it
)
media_plays(
  <common>,
  ts           TIMESTAMPTZ NOT NULL,  -- when the play ended (Spotify's convention)
  media_kind   VARCHAR NOT NULL CHECK (media_kind IN ('track','episode','video','audiobook')),
  artist       VARCHAR, track VARCHAR, album VARCHAR,
  uri          VARCHAR,
  ms_played    BIGINT NOT NULL,
  platform     VARCHAR,
  shuffle      BOOLEAN,
  skipped      BOOLEAN
)
transactions(
  <common>,
  ts           TIMESTAMPTZ NOT NULL,
  amount       DECIMAL(18,2) NOT NULL, -- negative = money out
  currency     VARCHAR NOT NULL,       -- ISO 4217
  merchant_raw VARCHAR,
  merchant     VARCHAR,                -- normalised
  counterparty VARCHAR,                -- transfers: the other party's name as exported
  contact_id   VARCHAR,                -- → contacts.id (WP-12)
  category     VARCHAR,
  account      VARCHAR NOT NULL,
  balance      DECIMAL(18,2),
  kind         VARCHAR NOT NULL CHECK (kind IN ('card','transfer','fee','topup','exchange','refund','other'))
)
events(
  <common>,
  ts           TIMESTAMPTZ NOT NULL,
  kind         VARCHAR NOT NULL,       -- dotted namespace: 'youtube.watch', 'google.search', 'chrome.visit', 'shell.command'
  title        VARCHAR,
  url          VARCHAR
)
locations(
  <common>,
  ts           TIMESTAMPTZ NOT NULL,
  end_ts       TIMESTAMPTZ,            -- visits and segments
  lat DOUBLE NOT NULL, lon DOUBLE NOT NULL,
  accuracy_m   DOUBLE,
  place_name   VARCHAR,
  kind         VARCHAR NOT NULL CHECK (kind IN ('ping','visit','segment'))
)
contacts(                              -- derived, no provenance columns
  id           VARCHAR PRIMARY KEY,    -- uuid4 hex
  display_name VARCHAR NOT NULL,
  aliases      VARCHAR[] NOT NULL,
  identities   VARCHAR[] NOT NULL,     -- sender_id / counterparty values merged into this contact
  sources      VARCHAR[] NOT NULL,
  merged_into  VARCHAR,                -- → contacts.id
  created_at   TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
)
imports(
  id                VARCHAR PRIMARY KEY,  -- uuid4 hex
  connector         VARCHAR NOT NULL,
  connector_version VARCHAR NOT NULL,
  path_hash         VARCHAR NOT NULL,     -- sha256 of the sorted (relative path, size) list of the export
  tz                VARCHAR NOT NULL,     -- IANA name used to resolve local times
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  rows_seen         BIGINT NOT NULL DEFAULT 0,
  rows_inserted     BIGINT NOT NULL DEFAULT 0,
  status            VARCHAR NOT NULL CHECK (status IN ('running','succeeded','failed'))
)
contact_decisions(a VARCHAR, b VARCHAR, decision VARCHAR CHECK (decision IN ('merge','reject')), decided_at TIMESTAMPTZ, PRIMARY KEY (a, b))  -- v2 (WP-12)
schema_meta(key VARCHAR PRIMARY KEY, value VARCHAR)   -- 'schema_version' = '2' after WP-12
```

Raw source tables (`raw.<connector>_<name>`) are **optional in v1**: a connector may write them through `ctx.raw(...)`, no acceptance criterion requires them.

### 4.3 Rules

- `ts` is timezone-aware and stored as UTC. Connectors resolve local times with `ctx.tz` (§5).
- `source_row_id` must **not** depend on the file name or the export date, so overlapping exports dedupe: use the export's own id when it has one; otherwise sha256 of the normalised record's identifying fields. For formats where identical records can legitimately repeat (two "ok" messages in the same minute), append the occurrence index among identical records **with the same timestamp**; the counter is kept for the current timestamp only, so memory stays bounded (non-adjacent identical records with the same timestamp may collapse — accepted).
- Nothing personal in logs. Log counts and ids, never content.
- Schema changes bump `schema_version` and add a migration in `core/store.py`.

### 4.4 Python surface (`sherd_core`)

```python
# models.py — one Pydantic model per fact table; connectors fill source_file, source_row_id and the
# table's own columns. id, source, import_id and imported_at are set by the store.
class Message(BaseModel): ...
class MediaPlay(BaseModel): ...
class Transaction(BaseModel): ...
class Event(BaseModel): ...
class Location(BaseModel): ...
Row = Message | MediaPlay | Transaction | Event | Location

# store.py — the only module that writes.
class Store:
    @classmethod
    def open(cls, path: Path, *, read_only: bool = False, sandboxed: bool = False) -> "Store": ...  # creates + migrates when writable; sandboxed = no external access, locked config
    def begin_import(self, connector: str, connector_version: str, path_hash: str, tz: str) -> str: ...
    def upsert(self, import_id: str, source: str, rows: Iterable[Row], batch_size: int = 5000) -> UpsertStats: ...
    def finish_import(self, import_id: str, status: Literal["succeeded", "failed"]) -> UpsertStats: ...  # returns the ledger
    def query(self, sql: str, params: Sequence[object] = ()) -> pa.Table: ...  # parametrised; reads only (see below)
    def table_counts(self) -> dict[str, int]: ...
    def export(self, sql: str, params: Sequence[object], path: Path, fmt: Literal["csv", "parquet"]) -> int: ...  # COPY a read to a file (WP-16)
    def interrupt(self) -> None: ...                                         # cancel the running query (from another thread)
    def close(self) -> None: ...

@dataclass(frozen=True)
class UpsertStats:
    seen: int
    inserted: int

# catalog.py — loads catalog.yaml (table/column descriptions for the agent and the docs).
def load_catalog(path: Path | None = None) -> Catalog: ...
```

- **Import ledger:** `upsert` commits per batch and updates `imports.rows_seen`/`rows_inserted` in the same transaction as each batch, so the ledger is exact even when an import fails halfway (the rows already written stay; a re-run inserts the rest). `finish_import` sets `status` and `finished_at` and returns the ledger. Opening a writable store marks any import still `running` as `failed` (DuckDB's file lock means no other writer can be running).
- **Read-only guarantee:** `query()` on a writable store refuses non-SELECT statements to catch mistakes, but it is not a security boundary (a SELECT can still call side-effecting functions such as `nextval`). Untrusted SQL — the agent, the MCP server — must run on `Store.open(path, read_only=True)`.

Default database path: `$SHERD_HOME/life.duckdb`, `SHERD_HOME` defaulting to `~/.sherd`.

---

## 5. Contract B — Connector interface **\[DECIDED, v1\]**

```python
@dataclass(frozen=True)
class DetectResult:
    confidence: float          # 0..1
    reason: str                # short, no personal content

@dataclass(frozen=True)
class ImportContext:
    export_root: Path
    tz: ZoneInfo               # resolved before parse; see below
    self_identities: frozenset[str]   # names/numbers that are "me" (sets is_from_me)
    raw: RawSink | None = None # optional raw-table writer

class Connector(Protocol):
    id: str                    # 'whatsapp'
    version: str               # bump when output changes
    display_name: str
    def detect(self, path: Path) -> DetectResult: ...                    # fast: no full parse
    def parse(self, path: Path, ctx: ImportContext) -> Iterator[Row]: ... # streams; never loads a whole file
    def fixtures(self) -> list[Path]: ...                                 # fixture variant directories
```

- **Registration:** a subpackage of `sherd_connectors` exposing `CONNECTOR: Connector` (§3.3).
- **Timezone resolution** (CLI owns it, connectors only read `ctx.tz`): `--tz` flag → the `tz` of the previous import of the same connector → the system local zone; the CLI prompts only when interactive and the export has no zone information. Fixtures declare their zone in `meta.json`, so demo mode and tests never prompt.
- **Fixtures:** `fixtures/<connector_id>/<variant>/` holds the synthetic export under `export` (a file or directory; `meta.json` may set `"export": "<relative path>"` to point elsewhere inside the variant, e.g. a single `.zip`), `meta.json` (`{"tz": "...", "self_identities": [...], "description": "..."}`) and `expected.jsonl` (the canonical rows, sorted by `source_row_id`, with store-set fields omitted). Each connector also ships `FORMAT.md` in its package directory documenting the export and where to get it.
- **Golden tests:** `sherd_connectors.testing.assert_golden(connector, variant_dir)` parses the fixture and compares to `expected.jsonl`; `--update-goldens` rewrites them. A connector PR without golden tests is rejected.
- `sherd dig` runs `detect` on all registered connectors and picks the best; ties within 0.1 or confidence < 0.5 prompt the user (or fail with a clear message when not interactive).
- **Demo mode** (`sherd demo`) builds `$SHERD_HOME/demo.duckdb` from every connector's fixtures plus the synthetic generator's "demo" profile, so the UI works with no exports. Screenshots and the landing page use it.
- **Malformed records:** a record missing a required canonical field is skipped and counted (the count is logged once, no content); one bad row never fails a real export.
- **Synthetic generator** (`sherd_connectors.synth`, WP-03): deterministic (seeded) generator of canonical rows at a given scale (`demo` ≈ 50k rows; `bench` ≈ 2M rows) and of large raw export files for the memory and Rust benchmarks. Connectors add their raw-file generators in their own `synth_<id>.py` module.

---

## 6. Contract C — Insights ("digs") **\[DECIDED, v1\]**

An insight is a small, testable module: a parametrised SQL query over the canonical tables + a Vega-Lite chart spec + a one-line narrative template. No LLM involved.

```python
@dataclass(frozen=True)
class DigParams:
    date_from: date | None = None
    date_to: date | None = None
    top_n: int = 10
    granularity: Literal["day", "week", "month", "year"] = "month"
    tz: str = "UTC"                     # IANA zone for local-time bucketing (hours, days); callers pass the user's zone
    currency: str | None = None         # money digs: ISO 4217; None = the currency with the most transactions in range

@dataclass(frozen=True)
class Headline:
    label: str
    value: float | int | str
    unit: str | None = None
    delta: float | None = None          # vs the previous equal-length period, when meaningful

@dataclass(frozen=True)
class DigResult:
    data: pa.Table
    chart: dict[str, Any] | None        # Vega-Lite v5 spec with inline data values and the sherd theme applied
    narrative: str
    headline: Headline | None
    text_summary: str                   # accessible text alternative of the chart

class Dig(Protocol):
    id: str                             # 'messages.volume_by_contact'
    title: str
    requires: list[str]                 # tables that must have rows, e.g. ['messages']
    def compute(self, store: Store, params: DigParams) -> DigResult: ...
```

- **Registration:** a module in `sherd_insights.digs` exposing `DIGS: list[Dig]` (§3.3).
- Theme: `sherd_insights/theme/sherd.vega.json` (WP-14a), applied by `charts.apply_theme(spec)`; the web UI and PNG export use the same file.
- Insights are discoverable: `sherd show` lists every dig whose `requires` tables have data.

**Launch set of digs:**

Messages
- Volume per contact per month/year; top-N contacts by year (bump chart: how your circle changed).
- Response-time distribution per contact, and *your* median reply time vs theirs.
- Conversation starters: who initiates, per chat.
- Activity heatmap by hour × weekday ("night-owl index").
- Longest streaks and longest silences per contact.
- Emoji and word usage over time (top-N, excluding stopwords, multilingual-safe).

Music
- Listening minutes per month; top artists/tracks per year; "discovery rate" (share of plays from artists first heard that month).
- Seasonality: winter vs summer; morning vs night.
- Skip rate and "one-hit obsessions" (tracks played >30× in one week).

Money
- Spend by category and merchant per month; recurring charges (same merchant, ~same amount, ~30-day period) → **subscription finder** with annual cost.
- Income vs spend, savings-rate trend; weekday vs weekend spending.
- "Delivery index": food-delivery spend trend and its share of total spend.

Cross-source
- **Life timeline:** one merged, zoomable timeline of everything with density bands.
- "Soundtrack of a period": top tracks during any selected date range.
- Fun, not creepy, correlations (spend vs listening minutes by week; message volume vs YouTube time). Always labelled as correlation.
- Year-over-year deltas for every headline stat (trends page).

Wrapped
- `sherd wrapped 2026` → PNG cards (Vega-Lite → PNG via `vl-convert`) with the year's headline stats, generated locally, watermarked with the sherd owl. No raw data on the cards.

---

## 7. Contract D — Agent **\[DECIDED, v1\]**

- Input: question + catalog (descriptions from `catalog.yaml` + row counts + date ranges) + available digs. Output (validated JSON): `{"sql": ...}` or `{"dig": id, "params": {...}}` or `{"refuse": reason}`.
- Prefer a dig when one matches; fall back to SQL. Execute on a **read-only** DuckDB connection with a row and time budget (defaults: 10 000 rows, 10 s). Show the SQL and row count, then a short answer, then a chart when the result is chartable.
- A golden-question suite of ≥20 questions with expected result shapes runs in CI against the demo database with a stub provider; a nightly job uses a real one.
- **`--offline`** hard-fails any non-loopback network call. Loopback (`127.0.0.1`, `::1`, `localhost`) stays allowed so Ollama works offline. Token accounting is printed per question for remote providers.

---

## 8. Integrations

Ordered by value ÷ effort. First three are in v1.

| Integration | Why | Effort |
| --- | --- | --- |
| **MCP server** | Any AI client (Claude Desktop, Cursor, OpenClaw) can query your sherd. | Small: wraps Contract D |
| **Wrapped PNG export** | The viral loop. | Small once digs exist |
| **CSV/Parquet export + "open in DuckDB/Jupyter"** | Power users, r/dataisbeautiful posts. | Tiny |
| **Obsidian daily-notes export** | "On this day" notes with messages/plays/spend. | Medium (Phase 2) |
| **Telegram bot** | Ask from your phone; daily "10 years ago today" card. | Medium (Phase 2) |
| **ICS calendar feed** | Life timeline as an overlay calendar. | Small (Phase 2) |
| **Grafana datasource** | DuckDB is directly readable; ship a dashboard JSON. | Tiny (Phase 2) |
| **Shell-history connector** | zsh/bash with timestamps; on-brand. | Small (Phase 1) |

---

## 9. Design brief (Claude, WP-14a/14b/15)

- **Identity:** archaeology of the self. Warm dark palette (fired-clay terracotta accent on charcoal), a small owl mascot holding a sherd. Typography: a humanist sans for UI, monospace for SQL. No "AI" iconography. Fonts are vendored (no font CDN).
- **Voice:** calm, precise, light humour in empty states ("No sherds yet. Start a dig.").
- **Pages (web UI v1):** Home (headline stats + "start here" digs), Messages, Music, Money, Timeline, Ask (chat with SQL always visible), Wrapped, Settings (connectors, LLM provider, privacy status with a big green "0 bytes sent" counter).
- **Design tokens** live in one CSS file; components are Jinja macros. Charts use one Vega-Lite theme file so PNG export matches the UI.
- **Accessibility:** keyboard navigable, 4.5:1 contrast, charts have text summaries.

---

## 10. Build order and work packages

### 10.1 Sequence

**Sprint 0 (foundations):** WP-01 → WP-02 → WP-03, plus WP-14a in parallel (depends on nothing). WP-01 doubles as the first model trial (§10.4).

**Sprint 1 (fan out):** connectors and digs in parallel. Milestone **Demo Mode**: `sherd demo && sherd show` renders a full dashboard from synthetic data.

**Sprint 2:** the agent, the MCP server, the web UI on real data.

**Sprint 3:** Wrapped, packaging, docs, launch.

### 10.2 Work packages

Paths are relative to `packages/<pkg>/src/<import name>/` unless they start at the repo root.

| WP | Title | Owns | Depends on | Acceptance |
| --- | --- | --- | --- | --- |
| 01 | Repo skeleton + gate + CI | root config, `scripts/`, `.github/`, `.pre-commit-config.yaml`, every `packages/*/pyproject.toml` and package skeleton (`__init__.py`, `py.typed`, one smoke test), `sherd_cli/main.py` with command discovery | – | `uv sync` works; `scripts/check` green locally and in CI; PII scan and network guard tested (§11.3–4). |
| 02 | Core: schema, models, store, catalog | `packages/core` | 01 | Contract A tables and `schema_meta`; Store API of §4.4; idempotency test (import twice → 0 inserted); overlapping-import test; read-only `query`; `catalog.yaml` loader. |
| 03 | Connector framework + synth generator + `sherd demo`/`sherd dig` | `sherd_connectors/{base,registry,testing}.py`, `sherd_connectors/synth/`, `fixtures/README.md`, `sherd_cli/commands/{demo,dig}.py` | 02 | Contract B; golden harness; deterministic generator (`demo`, `bench` profiles); `sherd demo` builds a DB; `sherd dig` detect/tie/low-confidence paths tested. |
| 04 | WhatsApp connector (Python) | `sherd_connectors/whatsapp/`, `fixtures/whatsapp/` | 03 | iOS + Android + 4 locales + multi-line + media + system messages; 8 fixtures; golden tests. |
| 05 | Spotify connector | `sherd_connectors/spotify/`, `fixtures/spotify/` | 03 | Extended streaming history (audio + video files) and account-data `StreamingHistory*.json`; dedup across overlapping exports. |
| 06 | Google Takeout: YouTube + Search + Chrome | `sherd_connectors/google_takeout/`, `fixtures/google_takeout/` | 03, §12.1 | JSON and HTML variants for watch/search history; Chrome history; locale-safe timestamps. |
| 07 | Bank CSV: Revolut + YAML mapper | `sherd_connectors/bank_csv/`, `fixtures/bank_csv/` | 03 | Revolut columns; YAML column-mapping format so any bank CSV needs no code; merchant normalisation. Banks beyond Revolut per §12.4. |
| 08 | Shell history connector | `sherd_connectors/shell_history/`, `fixtures/shell_history/` | 03 | zsh extended history, bash with `HISTTIMEFORMAT`, atuin SQLite. |
| 09 | Insights framework + charts + 6 message digs | `sherd_insights/{base,registry,charts}.py`, `digs/messages_*.py`, `sherd_cli/commands/show.py` | 02, 03, 14a | Contract C; each dig tested on the demo DB; theme applied; `sherd show --list`. |
| 10 | Music + money digs (incl. subscription finder) | `digs/music_*.py`, `digs/money_*.py` | 09 | 8 digs; subscription-finder precision test on fixtures. |
| 11 | Cross-source digs + trends data | `digs/cross_*.py`, `digs/trends.py` | 09, 10 | Life timeline < 1 s on the `bench` profile (2M rows); YoY deltas. |
| 12 | Entity resolution (contacts) | `sherd_core/entities.py`, `sherd_cli/commands/contacts.py` | 02, 04, 07 | Fuzzy-merge names/phones across WhatsApp and bank transfers; user-confirmable merges; never auto-merge below threshold. |
| 13 | Agent: LLM client, providers, SQL guard, ask | `packages/agent`, `sherd_cli/commands/ask.py` | 02, 09 | Contract D; 20 golden questions in CI with a stub provider; `--offline` enforced by a network-blocking test that allows loopback. |
| 14a | Design system: tokens, owl, chart theme, copy | `sherd_web/static/{tokens.css,owl.svg,owl-mark.svg,fonts/,vendor/}`, `sherd_web/static/design/`, `sherd_insights/theme/`, `sherd_web/copy.yaml` | – | Tokens (contrast-checked), owl SVG, Vega theme JSON, a static preview page rendering sample charts with the theme, empty-state and onboarding copy. |
| 14b | Web shell | `sherd_web/{app.py,templates/base*,templates/macros/}`, `sherd_cli/commands/web.py` | 09, 14a | Layout, nav, empty states; renders the demo DB; no external requests (CSP test). |
| 15 | Web pages | `sherd_web/routes/`, `sherd_web/templates/pages/` | 11, 13, 14b | All pages render on the demo DB; Lighthouse a11y ≥ 90; CSP test. |
| 16 | CLI + TUI polish | `sherd_cli/tui/`, `sherd_cli/commands/export.py`, help/error texts | 09, 13 | `sherd dig/ask/show/demo/wrapped/export`; TUI dashboard; helpful errors. |
| 17 | MCP server | `packages/mcp` | 13 | stdio MCP with read-only query, describe, insights; tested with an MCP client. |
| 18 | Wrapped generator + Wrapped page | `sherd_insights/wrapped.py`, `sherd_cli/commands/wrapped.py`, `sherd_web/routes/wrapped.py`, `sherd_web/templates/pages/wrapped.html` | 10, 11, 14a | 6 PNG cards from the demo DB; deterministic; no raw text by default; registers `sherd_web/static/fonts/ttf/` with vl-convert (it ignores woff2) and a test proves the PNG uses Fira Sans. |
| 19 | Rust WhatsApp parser | `crates/sherd-wa` | 04 | Same golden tests through PyO3; ≥10× faster on a 1 GB synthetic export; wheels built in CI. |
| 20 | Packaging + install + docs + landing | `docs/`, `.github/workflows/release.yml`, install script | 15, 16 | `uv tool install sherd-cli`, `pipx install sherd-cli` and a `curl … \| sh` install script work on a clean macOS and Linux machine; docs site builds. |
| 21 | Launch kit | `docs/launch/` | 18, 20 | Show HN text, Reddit posts, 60-s GIF script, FAQ, Wrapped share flow tested. |

Phase 2 WPs (after launch): Google location (all 3 formats), Apple Health/Strava, Netflix, Instagram/Facebook, Obsidian export, Telegram bot, ICS feed, Grafana dashboard, community connector template + `sherd new-connector` scaffold.

### 10.3 Parallelism map

```
Sprint 0:  01 ─► 02 ─► 03                ‖   14a (design system)
Sprint 1:  04 05 06 07 08 (connectors)   ‖   09 ─► 10 ─► 11 (insights)   ‖   14b (web shell)
Sprint 2:  12 13 17 (agent/MCP)           ‖   15 (web pages)              ‖   19 (Rust)
Sprint 3:  16 18 20 21
```

### 10.4 Model routing by tier

Work is dispatched to a **tier**, never to a model by name; this table is the only place that names a model, so a model release changes one row.

| tier | work | current model (since, evidence) |
| --- | --- | --- |
| `deep` | contracts and data model (WP-02/03), cross-cutting builds, design with taste (WP-14a/14b/15), plan critiques | Claude Opus 5.5, high (carried over from the Ema S16/S17a head-to-head, 2026-09-25) |
| `standard` | well-specified builds with logic (connectors, digs, agent pieces); fix rounds that need judgement | Codex `gpt-6.1-sol`, medium (2026-10-02, T1); reviewer of a Codex build: Claude Sonnet 5.5 |
| `light` | mechanical work: scaffolding, config, CI, docs, tests from a list, fix rounds from a findings list | Codex `gpt-6.1-sol`, low (2026-10-02, T1); `gpt-6-luna` stays a candidate for a findings-list fix trial |

The coordinator is Opus and is not a tier. A row changes only on evidence: when a provider releases a model, or two WPs in a row from a tier need more than one fix round, the coordinator runs one trial — candidate and current model on the same WP at the same `dev` commit, a blind cross-model review, cost from the session logs. The cheapest model that matches the best review wins; the row records the date and the trial.

**Trial T1 (2026-10-02):** WP-01 built four times from the same `dev` commit and spec. Result: `gpt-6.1-sol` medium best (blind review 29/30; ≈66k fresh input, 10k output) and merged; Sonnet 5.5 second (24/30; IBAN/phone scanner gaps; fastest; ≈58k cache-write, 22k output); `gpt-6-luna` third (17/30; static-import bypass, unix sockets blocked, thin tests; *more* tokens than sol: ≈98k fresh, 18k output); Haiku 4.5 failed acceptance (`sherd version` broken, a spec rule dropped, while reporting success; ≈137k cache-write, 50k output). Caveats: one task, and the blind reviewer was `gpt-6.1-sol` low; the coordinator re-ran the gates and confirmed the decisive scanner defects independently.

**Budget:** Codex work stops when the weekly Codex limit reaches 30 % used (checked from the Codex session logs before every Codex dispatch).

---

## 11. Review protocol

A WP is mergeable into `dev` only if all are true:

1. Touches only files owned by its WP (or has an explicit coordinator waiver).
2. Tests added for every new behaviour; golden fixtures for connectors; `scripts/check` green (and CI green once the GitHub repo exists).
3. **No real personal data** anywhere. Fixtures are synthetic and use reserved values only: phone numbers in the fictional `555-01xx` range (`+1 555 0100`–`0199`), emails at `example.com/.org/.net` or `.test`/`.invalid` domains, IBAN-shaped strings with the non-country code `XX`. `scripts/pii_scan.py` (pre-commit and `scripts/check`) rejects any other phone/email/IBAN pattern in tracked files, except exact strings listed in `.pii-allowlist`.
4. No network calls added outside `sherd_agent.providers`: the test suite blocks non-loopback sockets, and a static test rejects network-library imports elsewhere.
5. Streaming, not loading: connectors iterate; memory test on a 500 MB synthetic file stays under 500 MB RSS.
6. Every SQL touching canonical tables is parametrised and read-only unless in `core/store.py`.
7. Types pass `mypy --strict`; no `# type: ignore` without a reason comment.
8. Docs: connector WPs ship `FORMAT.md`; digs appear in the generated index automatically.
9. For WPs 09–18 the reviewer runs `sherd demo && sherd show` (or the web UI) and confirms nothing regressed visually.
10. The review ends with an explicit verdict: **APPROVE / REQUEST CHANGES** plus the top remaining risk.

Light workflow: one build, one cross-model review, one fix round; the coordinator verifies small fixes from the diff.

---

## 12. Open decisions

1. **\[OPEN\] Licence.** Recommendation: **Apache-2.0** for v1. Blocks WP-06 (vendoring) and WP-20; the repo has no LICENSE file until decided.
2. **\[OPEN\] Default LLM path.** Recommendation: detect Ollama; if absent, guide to BYO key; never require either for Show. Blocks WP-13.
3. **\[DECIDED\] Web UI framework:** HTMX + Jinja unless WP-14b shows a hard limitation.
4. **\[OPEN\] Banks beyond Revolut** for launch (ING, BT testable locally; N26/Wise popular). The YAML mapper ships regardless. Blocks WP-07's bank list only.
5. **\[OPEN\] Wrapped defaults:** which 6 cards; contact names as initials by default (recommended). Blocks WP-18.
6. **\[OPEN\] Location in v1?** Recommendation: no; ship two weeks after launch.
7. **\[OPEN\] Name reservations:** `sherd-cli` (PyPI), `sherd-dev` (GitHub), `sherd-wa` (crates.io), a domain — Vlad, all on the same day.

---

## 13. Review rounds on this plan

**Round 1 — scope.** Ask was the centre and Show "later". Most users will never type a question → three verbs; insights got a contract and WPs; Wrapped moved into v1.

**Round 2 — parallelism.** WPs shared files → contracts frozen in Sprint 0, per-package ownership, contract-change WPs, parallelism map.

**Round 3 — privacy and safety.** → synthetic-only fixtures, PII hook, socket-blocking test, "0 bytes sent" counter, initials-only Wrapped default.

**Round 4 — research.** → Location to Phase 2; package renamed `sherd-cli`; reuse HPI/Dogsheep after a licence check.

**Round 5 — cool factor.** → MCP server, cross-source digs, Phase-2 integrations, shell-history connector.

**Round 6 — handoff readiness (coordinator, 2026-10-02).** Contracts had `...` columns and no id rule, so parallel engineers would have guessed; several "parallel" WPs still shared files (connector entry points in one pyproject, one fixtures dir, a hand-edited insights index, `sherd demo` inside the CLI package); `--offline` would have blocked Ollama; "ask the user for the timezone" broke demo mode and CI; no WP built the large synthetic data that three acceptance criteria need; the PII hook would have rejected the WhatsApp fixtures. → Contracts completed (§4–§7), discovery-based registration (§3.3), loopback-only offline, `ImportContext.tz`, synth generator in WP-03, reserved-value PII rules, WP-14 split, branch model (§15), model routing (§10.4).

Remaining known weaknesses: entity resolution (WP-12) is hard and could move to Phase 2 if it slips; text-to-SQL quality depends on the catalog descriptions, which need real writing effort; Wrapped card design is a design task, not a chart export.

---

## 14. Next steps

1. Vlad: request Google Takeout (YouTube, Search, Chrome), export three WhatsApp chats, request Spotify extended history, download 12 months of Revolut CSV. Keep them in `~/sherd-private/` (outside the repo).
2. Vlad: reserve the names (§12.7), create `sherd-dev/sherd` on GitHub, push `main` and `dev`, make `dev` the default branch, and protect both (§15).
3. Coordinator: Sprint 0 — WP-01 as trial T1, then WP-02 and WP-03; WP-14a in parallel.
4. Decide §12 items 1, 2, 4 before Sprint 1 fans out.

Definition of "we have a product": `uv tool install sherd-cli && sherd demo && sherd web` opens a dashboard that makes someone say "wait, I want this with *my* data."

---

## 15. Branches and releases **\[DECIDED\]**

- **`dev`** is the integration branch and the GitHub default. Every WP worktree branches from `dev` and merges back into `dev` after review (by PR once the GitHub repo exists; CI must pass).
- **`main`** is the release branch. It only moves by fast-forward from `dev`, done by Vlad at the end of a sprint or for a release. Nobody commits to `main` directly.
- **Releases:** a `v*` tag on `main` runs `release.yml` (PyPI `sherd-cli`, maturin wheels for `sherd-wa`). The end of Sprint 0 is tagged `v0.0.1` to exercise the flow.
- **Urgent fixes:** branch from `main`, merge into `main`, then merge `main` back into `dev`.
- **Branch names:** `wp<nn>-<slug>` for WP work (trial builds: `wp01-<model>`), `fix/<slug>` for fixes, `docs/<slug>` for docs-only changes.
- The primary checkout stays on `dev`; all engineering happens in Orca worktrees. Until the GitHub repo exists, the coordinator merges reviewed WP branches into `dev` locally (after re-running `scripts/check`) and commits `PLAN.md`/`AGENTS.md` updates directly on `dev`.
