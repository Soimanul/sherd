# Show HN: sherd – explore the data exports you already own

## First comment

I built sherd to put old exports back to work. `sherd dig PATH` detects and imports them into a local DuckDB file; `sherd show` gives you charts and prebuilt insights; `sherd ask "What changed this year?"` turns a question into a read-only query and shows the SQL. The local web dashboard and six PNG Wrapped cards offer another way to browse.

The five connector families currently cover WhatsApp chat text, Spotify listening history, Google Takeout's YouTube/Search/Chrome histories, Revolut CSV and mapped bank CSV, and timestamped zsh/bash or Atuin history. There are 22 built-in digs. The stack is Python, DuckDB, and a Rust WhatsApp parser. The licence is Apache-2.0.

Try it on synthetic data in about a minute (macOS or Linux):

```sh
uv tool install sherd-cli   # or: pipx install sherd-cli
sherd demo && sherd web --demo
```

[Demo dashboard](images/demo-dashboard.png) · [example Wrapped card](images/wrapped-2025-1.png). These images contain made-up data.

Dig, Show, Web, Wrapped and exports run locally. Ask prefers Ollama on loopback. Anthropic and OpenAI require your API key and consent; the planning request can include your question, schema, row counts and date ranges, and a follow-up can include result rows. `--offline` on Ask blocks non-loopback connections. MCP can return queried data to the client you configure. The tool has no account or telemetry.

Still missing: Google location history, Apple Health/Strava, Netflix, Instagram/Facebook, Obsidian export, Telegram bot, ICS feed, Grafana dashboard, and a community connector template/scaffold. I would also like more real-export format testing.

Which export would you try first, and where would the import or chart fall short?

TODO(vlad): confirm PyPI availability and replace this with the public docs URL once Pages is on. Add one sentence of personal motivation if wanted.
