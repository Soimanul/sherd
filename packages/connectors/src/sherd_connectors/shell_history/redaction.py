"""Conservative secret-value redaction; no command content is logged."""

import re

REDACTED = "«redacted»"
_VALUE = r"""(?:"(?:\\.|[^"\\])*"|'[^']*'|[^\s;"']+)"""
_PATTERNS = [
    re.compile(rf"(?P<prefix>--(?:password|token|secret|api-key)(?:=|\s+)){_VALUE}", re.I),
    re.compile(rf"(?P<prefix>(?<!\S)-p){_VALUE}"),
    re.compile(r"""(?P<prefix>Authorization:\s*Bearer\s+)[^\s"']+""", re.I),
    re.compile(r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+(?=@)", re.I),
    re.compile(r"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]+\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def redact(command: str) -> tuple[str, int]:
    """Return the safe command and the number of replaced secret values."""
    count = 0
    assignment = re.compile(
        rf"(?P<prefix>\b\w*(?:token|secret|password|passwd|api_?key|auth)\w*=){_VALUE}", re.I
    )

    def environment(match: re.Match[str]) -> str:
        nonlocal count
        safe, replaced = assignment.subn(lambda m: m["prefix"] + REDACTED, match[0])
        count += replaced
        return safe

    command = re.sub(
        r"""\b(?:export|env)\s+(?:"(?:\\.|[^"\\])*"|'[^']*'|[^;"'\n])+""",
        environment,
        command,
    )
    for pattern in _PATTERNS:
        command, replaced = pattern.subn(
            lambda match: match.groupdict().get("prefix", "") + REDACTED, command
        )
        count += replaced
    return command, count
