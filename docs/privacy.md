# Privacy

Your exports and DuckDB database stay on your machine. sherd has no account, cloud sync, telemetry or live service polling. Dig, Show, Web, Wrapped and export are local operations. The web server binds to loopback.

Ask prefers local Ollama. A remote provider is used only if available, selected and consented to; the first request discloses its payload, and usage is shown afterward. `--offline` blocks non-loopback connections during Ask. The MCP server is read-only, but an MCP client can receive the answers it requests.

The install script contacts Astral only when uv is missing, and the package index when installing. It does not send personal data.
