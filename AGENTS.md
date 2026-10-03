# AGENTS.md — rules for agents working on sherd

Read this file, then `PLAN.md` §3–§7 (architecture and contracts) and the row of your WP in §10.2.
`PLAN.md` is the source of truth; if it is wrong or silent on something your work depends on,
stop and ask the coordinator instead of guessing.

## Ownership
- You own only the paths listed for your WP (PLAN.md §10.2, §3.3). Do not edit anything else.
  If you must, stop and ask.
- Built-in connectors, digs and CLI commands are discovered from their modules; never add them to
  a central list.
- In a package shared with other WPs, you may only append your own dependency lines to its
  `pyproject.toml`.

## Data and privacy
- **No real personal data in the repo, ever.** Fixtures are synthetic and use reserved values
  only: phones `+1 555 0100`–`0199`, emails at `example.com/.org/.net` or `.test`/`.invalid`,
  IBAN-shaped strings starting with `XX`. `scripts/pii_scan.py` enforces this.
- Logs carry counts and ids, never message text, names, amounts or locations.
- No network access outside `sherd_agent.providers`. Tests block non-loopback sockets.

## Code
- Python 3.12, `uv`. Streaming over loading. Parametrised SQL; only `sherd_core.store` writes.
- `mypy --strict` clean; a `# type: ignore` needs a reason comment.
- Tests cover changed behaviour. Connectors ship golden fixtures and `FORMAT.md`.
- Small, coherent commits in Conventional Commits style (`feat(core): …`, `fix(whatsapp): …`).
  No AI attribution or co-author footers.

## The gate
- `scripts/check` is the one gate: format check, lint, types, PII scan, tests. CI runs exactly it on
  every PR, and a PR merges only when CI is green.
- On this machine, run only the tests your change touches (plus format, lint and types on the files
  you changed); do not run the full suite locally — CI does that once per PR. Report exactly what you
  ran and its real result. A skipped or zero-test run is not a pass.

## Machine resources (shared Mac, 16 GB)
- One heavy test run at a time across all projects: before a long or memory-heavy run (benchmarks,
  large synthetic files, full suites), check `pgrep -fl "pytest|cargo test"` and wait if another runs.
- Quit any app you started for automation (browsers, etc.) when done; no stray `caffeinate`.
- Heavy runs only while the Mac is on AC power (`pmset -g batt`).

## Messages to the coordinator
- No heartbeat messages. Send an `ask` only when blocked, and exactly one completion message.

## Branches (PLAN.md §15)
- Work in your own Orca worktree on a branch from `dev`. Never commit to `dev` or `main`, never
  switch the primary checkout's branch, never push unless told to.

## Finishing
- When done: everything committed on your branch, `scripts/check` green, and a short report that
  maps each acceptance criterion to the test or evidence that proves it.
- If blocked, ask the coordinator one clear question and wait for the answer.
