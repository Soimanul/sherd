# Ask and LLM providers

`sherd ask "question"` plans a read-only query, shows the SQL and returns an answer. The default order in the shipped `providers.toml` is **Ollama → Anthropic → OpenAI**. Ollama runs on your machine at `127.0.0.1:11434`; use `--offline` to block non-loopback traffic while allowing Ollama.

Remote providers require an API key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) and one-time consent. Before the first request, sherd shows exactly what it will send. Consent is remembered in `$SHERD_HOME/config.json`; API keys stay in your environment. Use `--provider` and `--model` to override the choice, or set the `ask` object in that config file. The shipped defaults are in `sherd_agent/providers.toml`.

The planning request includes your question, schema, row counts, date ranges and available digs. A follow-up may include up to 50 result rows and 8 KiB of CSV. The CLI reports remote token and byte usage.
