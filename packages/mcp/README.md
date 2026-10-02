# sherd MCP

Run `sherd mcp --db /absolute/path/life.duckdb` or build synthetic data with
`sherd demo` and run `sherd mcp --demo`. With no options, the database is
`$SHERD_HOME/life.duckdb` (`SHERD_HOME` defaults to `~/.sherd`). The server uses
stdio only; diagnostics go to stderr. It never opens a network listener.

For Claude Desktop, add this entry to `claude_desktop_config.json`. For Cursor,
use the same entry in `.cursor/mcp.json` (or the user-level MCP configuration).
Use the absolute path to the installed `sherd` executable if it is not on the
client's PATH:

```json
{
  "mcpServers": {
    "sherd": {
      "command": "/absolute/path/to/sherd",
      "args": ["mcp", "--db", "/absolute/path/life.duckdb"]
    }
  }
}
```

For the demo, replace `args` with `["mcp", "--demo"]`.

Tools: `list_tables`, `describe(table)`, `query(sql)`, `list_insights`, and
`run_insight(id, params)`. Query accepts one read-only SELECT (including WITH),
uses a sandboxed read-only connection, and has a total 10-second execution
budget. Insight parameters follow `DigParams`: date_from, date_to, top_n,
granularity, tz, currency. Insights return headline, narrative, text_summary,
and records, without a Vega spec.

This is personal data: a client may forward tool results to a remote model.
Every response is capped at 200 rows and 32 KB of serialized JSON, including
escaping and envelope space. Cells longer than 500 characters are truncated
with `…`. `row_count` is the original count and `truncated` indicates any
row, byte, or cell cut. Query counts the original result before fetching its
bounded preview; both executions share the 10-second budget. Extremely wide
records may be omitted entirely to fit the byte cap. Long nested cells are
serialized and truncated as strings.
