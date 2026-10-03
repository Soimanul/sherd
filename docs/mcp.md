# MCP setup

The MCP server exposes read-only table descriptions, queries and insights over stdio. Build a demo first with `sherd demo`, then add this server configuration to Claude Desktop or Cursor:

```json
{
  "mcpServers": {
    "sherd": {
      "command": "sherd",
      "args": ["mcp", "--demo"]
    }
  }
}
```

Remove `--demo` to use your own local database. Restart your MCP client after editing its configuration. The client launches sherd locally; only the data you ask the client to query is returned to it.
