# Launch FAQ

## 1. What stays on my machine?

Imports, the DuckDB database, charts, local web dashboard, exports and Wrapped PNGs. There is no account, sync or telemetry. The web server binds to loopback. [Privacy details](../privacy.md).

## 2. What can leave it?

Ask can send a planning request and follow-up rows to a remote LLM after an API key and consent. A configured MCP client receives the answers it requests. Installing the package uses the network. [Ask details](../ask.md).

## 3. Can I use it without a remote LLM?

Yes. Dig, Show, Web, export and Wrapped need no LLM. Ask can use local Ollama; `--offline` rejects non-loopback traffic.

## 4. Which exports work?

Five connector families: WhatsApp text/ZIP, Spotify history JSON, Google Takeout YouTube/Search/Chrome history, Revolut or mapped bank CSV, and timestamped zsh/bash or Atuin history. See the [connector index](../connectors/index.md).

## 5. How do I get WhatsApp chats?

Use the chat's Export Chat action, then save the text or ZIP. Import chats separately or a folder of exports. See the [WhatsApp connector](../connectors/whatsapp/index.md).

## 6. How do I get Spotify history?

Request Extended Streaming History in Spotify's privacy settings; account-data history also works. See the [Spotify connector](../connectors/spotify/index.md).

## 7. How do I get Google history?

Download a Google Takeout archive containing YouTube, Search or Chrome history. Location History is not supported in v1. See the [Google Takeout connector](../connectors/google_takeout/index.md).

## 8. How do I get bank data?

Export Revolut transactions as CSV, or prepare a YAML mapping for another bank CSV. See the [bank CSV connector](../connectors/bank_csv/index.md).

## 9. How do I get shell history?

Point Dig at timestamped zsh extended history, bash history with timestamps, or an Atuin SQLite file. See the [shell history connector](../connectors/shell_history/index.md).

## 10. How do I set up Ollama?

Install Ollama, run its local service, and pull the shipped default model `qwen2.5-coder:7b`, or choose another model with `--model`. Then use `sherd ask "How many messages?" --offline`. Ollama listens on loopback by default in sherd's configuration. See [Ask and providers](../ask.md).

## 11. What do remote LLMs cost?

Your provider bills according to its own pricing and the tokens used. sherd displays usage; check your provider's current rates before opting in. No remote provider is needed for charts.

## 12. Does Windows work?

Windows is not supported in v1. Installation targets macOS and Linux. See [Install](../install.md).

## 13. How do I delete my data?

Stop sherd and delete the local DuckDB file, normally `$SHERD_HOME/life.duckdb` (or `~/.sherd/life.duckdb`). Delete `demo.duckdb` separately if you created it, plus any export or Wrapped PNG folders you chose. sherd does not delete the original service exports.

## 14. Why DuckDB?

A single local file supports SQL over the canonical tables and fast aggregations without operating a database server. CSV and Parquet exports give you a path back out.

## 15. Which MCP clients can connect?

The stdio server is documented for Claude Desktop and Cursor. Other stdio MCP clients may work; their configuration and handling of returned data are their own. See [MCP setup](../mcp.md).

## 16. How do I add a connector?

Implement the connector interface, ship synthetic golden fixtures and a `FORMAT.md`, then register via the `sherd.connectors` entry-point group or a discoverable subpackage. See [Write a connector](../write-a-connector.md).

## 17. Is Wrapped safe to share?

It uses initials instead of full contact names and hides currency amounts by default. It never includes message text. Dates and patterns may still identify you, so review every PNG. See the [share flow](wrapped-share-flow.md).

## 18. What licence is sherd under?

Apache-2.0. The [repository licence](../../LICENSE) has the full terms.
