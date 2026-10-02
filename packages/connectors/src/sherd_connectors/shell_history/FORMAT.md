# Shell history

Copy history files into a private export directory; sherd never reads your live history
implicitly. Close the shell or flush its history first (`fc -AI` in zsh, `history -a` in
bash). Copy `$HISTFILE` (usually `~/.zsh_history` or `~/.bash_history`). In zsh enable
`EXTENDED_HISTORY` before recording commands. In bash set `HISTTIMEFORMAT` before recording
history so saved files include epoch markers. Existing untimestamped commands cannot be
recovered as timed events.

For atuin, close its writers and copy `~/.local/share/atuin/history.db` (or your configured
history database) as `history.db`. Ensure any SQLite WAL is checkpointed before copying.
The connector opens the copy with a read-only SQLite URI and never modifies it.

Import a single file, a directory containing several files, or a zip, optionally with
subdirectories. Renamed files are recognized by timestamp markers or SQLite content. Detection reads at most 64 KB from each of
three candidate files. Plain zsh entries are skipped and counted. Bash files without a
timestamp marker in that sample have confidence zero.

## Mapping

All formats emit `events` with `kind="shell.command"`, UTC `ts`, redacted command `title`,
and NULL `url`. zsh `: epoch:duration;command` supplies seconds since the epoch and duration
in seconds, converted to `duration_ms`; lines ending in backslash continue the command,
preserving the backslash and newline. zsh Meta byte 0x83 escapes the next byte XOR 0x20;
unmetafy happens before UTF-8 decoding, with invalid bytes replaced. Bash `#epoch` starts
one command, including all following lines until the next marker; duration and exit are
NULL. zsh/bash ids hash shell, epoch, original command and its zero-based occurrence index
among identical records at the current timestamp in parse order; renaming exports does
not change identity. The counter clears whenever the timestamp changes. Identical
records at a timestamp revisited later may collapse into one stored row (accepted).

Atuin reads `id`, `timestamp` and `duration` (nanoseconds), `exit`, `command`, `cwd` and
`deleted_at`. Deleted rows are skipped when that column exists. Older schemas require id, timestamp
and command; absent duration, exit and cwd become NULL. Unsupported schemas are skipped. The original id becomes `source_row_id`; timestamps
are truncated to microsecond precision, duration becomes integer milliseconds. Metadata contains
only shell, duration_ms, exit and cwd. Home prefixes (`/home/<user>`, `/Users/<user>`, or the
current home) become `~`. `hostname` and `session` are never read or retained. Missing or
invalid required fields are skipped with counts logged, without command content.

## Privacy and limits

Secret option values (`--password`, `--token`, `--secret`, `--api-key`, attached `-p` for mysql, mysqldump, mysqladmin and mariadb),
secret environment assignments in `export`/`env`, bearer headers, URL credentials and known
GitHub/OpenAI/Slack/AWS token shapes become `«redacted»`. Counts alone are logged. This is
pattern-based redaction: arbitrary secrets in positional arguments cannot be identified.
Commands otherwise remain verbatim and can contain personal information; review exports.

Text zip members stream directly. By coordinator-approved exception, an atuin zip member
streams into a private temporary directory, is opened read-only, and is removed on success,
failure or generator closure. No archive paths are used as filesystem destinations.
Occurrence counters retain hashes only for the current timestamp, keeping memory
independent of the total number of identities across timestamps.
