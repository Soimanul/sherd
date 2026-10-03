# Wrapped

`sherd wrapped` renders six PNG cards locally. It uses the last full calendar year represented in your data unless you give a year. By default, contacts appear as initials and money amounts are hidden. Opt in with `--names` or `--amounts` only when you want those values on a card.

```sh
sherd wrapped --demo --out wrapped
sherd wrapped 2025 --names --amounts --out wrapped
```

Review cards before sharing them. No upload is part of the command.
