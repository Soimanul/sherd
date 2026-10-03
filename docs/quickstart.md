# Quickstart

Start with synthetic data and open the local dashboard:

```sh
sherd demo
sherd web
sherd show --list
```

Import one of [the supported exports](connectors/index.md), then inspect your digs:

```sh
sherd dig /path/to/export
sherd show
```

Ask questions with a local Ollama model, or choose a remote provider after reviewing consent:

```sh
sherd ask "What changed this year?" --offline
```

Create six shareable cards and export a table:

```sh
sherd wrapped --demo --out wrapped
sherd export --help
```

Run `sherd --help` or any command's `--help` for all options.
