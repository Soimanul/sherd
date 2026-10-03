# Quickstart

Start with synthetic data and open the local dashboard:

```sh
sherd demo && sherd web --demo
sherd show --list
```

Try the terminal dashboard and inspect contacts in the demo database:

```sh
sherd tui --demo
sherd contacts --demo
```

Import one of [the supported exports](connectors/index.md), then inspect your digs:

```sh
sherd dig /path/to/export
sherd show
```

`sherd dig /path/to/export` imports real data. `sherd show` reads that imported data; use `sherd show --demo` to inspect the demo instead. `sherd contacts` and `sherd tui` read real data unless you pass `--demo`.

Ask questions with a local Ollama model, or choose a remote provider after reviewing consent:

```sh
sherd ask "What changed this year?" --demo --offline
```

`sherd ask` needs a real database unless you pass `--demo`; it also needs an available model. Create six shareable cards from the demo and inspect export options:

```sh
sherd wrapped --demo --out wrapped
sherd export --help
```

Run `sherd --help` or any command's `--help` for all options.
