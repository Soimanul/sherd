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
- `scripts/check` is the one gate: format check, lint, types, PII scan, tests. CI runs exactly it.
  Run it before you report done, and report its real result. A skipped or zero-test run is not a pass.

## Branches (PLAN.md §15)
- Work in your own Orca worktree on a branch from `dev`. Never commit to `dev` or `main`, never
  switch the primary checkout's branch, never push unless told to.

## Finishing
- When done: everything committed on your branch, `scripts/check` green, and a short report that
  maps each acceptance criterion to the test or evidence that proves it.
- If blocked, ask the coordinator one clear question and wait for the answer.
