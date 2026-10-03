# Install sherd

Requires macOS or Linux and Python 3.12 or newer. The install script installs uv through Astral's official installer if needed, then installs the `sherd-cli` tool.

```sh
curl -LsSf https://raw.githubusercontent.com/Soimanul/sherd/main/scripts/install.sh | sh
```

If you already have uv:

```sh
uv tool install sherd-cli
```

Or use pipx:

```sh
pipx install sherd-cli
```

Then run `sherd demo && sherd web`. If your shell cannot find `sherd`, restart it or add `~/.local/bin` to `PATH`.

Pin a release with `SHERD_VERSION=0.1.0`; install from a local wheel directory with `SHERD_FIND_LINKS=/path/to/wheels`. Preview the script with `SHERD_DRY_RUN=1`.
