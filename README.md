# sherd

Point sherd at the exports you already own. See your life in charts, ask it questions, and make a Wrapped. Your data stays on your machine unless you choose otherwise.

```sh
curl -LsSf https://raw.githubusercontent.com/Soimanul/sherd/main/scripts/install.sh | sh
sherd demo && sherd web --demo
```

[Read the docs](https://soimanul.github.io/sherd/) for supported exports, local LLM setup and privacy details.

For development, install Python 3.12 and uv, then run `uv sync --all-packages`, `scripts/check`, and `uv run sherd version`.

Licensed under [Apache-2.0](LICENSE).
