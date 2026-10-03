# Reddit drafts

## r/dataisbeautiful

**Title:** [OC] A year of messages, music and spending from synthetic exports

**Body:** Lead image: [2025 Wrapped card](images/wrapped-2025-1.png). Other [cards](images/wrapped-2025-2.png) and the [dashboard](images/demo-dashboard.png) show the same demo database. All people, messages, plays and payments are **synthetic demo data**, not my history. I made the charts with sherd: Python connectors, DuckDB queries, Vega-Lite specs and vl-convert PNG rendering. The six Wrapped cards hide full contact names and currency amounts by default. `sherd demo && sherd web --demo` reproduces the dashboard. What comparison would make this more useful than a year-end total?

**Rule to respect:** [OC] needs original work, clear data source and tools; identify this as synthetic in the post, not just a comment.

## r/selfhosted

**Title:** I made a local-first dashboard for exports I already have

**Body:** [Demo dashboard](images/demo-dashboard.png). sherd imports WhatsApp, Spotify, Google Takeout history, bank CSV and shell history into a local DuckDB file. The web UI binds to loopback; there is no hosted account or server to manage. `sherd show` and Wrapped need no model. Ask prefers local Ollama, and `--offline` blocks non-loopback traffic. Remote providers require a key and consent. A read-only stdio MCP server lets a configured client query the database; that client can receive the answers it asks for. Try the synthetic data with `sherd demo && sherd web --demo`. I would welcome reports of export formats that fail detection.

**Rule to respect:** Describe the hosting and data flow plainly; avoid drive-by promotion and answer setup questions.

## r/datahoarder

**Title:** Turning old exports into a queryable local DuckDB file

**Body:** [Demo dashboard](images/demo-dashboard.png). If you keep service exports, sherd can import WhatsApp chat text/ZIP, Spotify JSON, Google Takeout YouTube/Search/Chrome histories, Revolut or mapped bank CSV, and timestamped shell/Atuin history. It stores canonical tables in one local DuckDB file. `sherd export --all --format parquet --out exports` writes Parquet; CSV is also supported. `sherd dig PATH` is idempotent, so rerunning the same export does not add duplicates. The screenshot uses synthetic data. Which export format in your archive would be worth supporting next?

**Rule to respect:** Keep the post focused on preservation, formats and reproducible local access; disclose the tool's authorship.
