"""Stream tracked text files and report masked non-reserved PII patterns."""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INTERNATIONAL = re.compile(r"(?<!\w)\+\d{1,3}(?:[ .()\-]*\d)+\)*(?!\w)")
ROMANIAN = re.compile(r"(?<!\w)07\d{2}[ .\-]?\d{3}[ .\-]?\d{3}(?!\w)")
IBAN = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?)\b",
    re.IGNORECASE,
)


def allowed_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[1].lower()
    return any(
        domain == base or domain.endswith("." + base)
        for base in ("example.com", "example.org", "example.net")
    ) or domain.endswith((".test", ".invalid", ".example", ".localhost"))


def allowed_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return re.fullmatch(r"1?55501\d{2}", digits) is not None


def valid_iban(value: str) -> bool:
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or compact.startswith("XX"):
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(
        str(ord(char) - ord("A") + 10) if char.isalpha() else char for char in rearranged
    )
    return int(numeric) % 97 == 1


def findings(line: str) -> Iterator[tuple[str, str]]:
    for match in EMAIL.finditer(line):
        if not allowed_email(match.group()):
            yield "email", match.group()
    for pattern in (INTERNATIONAL, ROMANIAN):
        for match in pattern.finditer(line):
            value = match.group()
            digits = re.sub(r"\D", "", value)
            if 8 <= len(digits) <= 15 and not allowed_phone(value):
                yield "phone", value
    for match in IBAN.finditer(line):
        value = match.group()
        while True:
            if valid_iban(value):
                yield "iban", value
                break
            if " " not in value:
                break
            value = value.rsplit(" ", 1)[0]


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line for line in path.read_text().splitlines() if line and not line.startswith("#")}


def scan_file(path: Path, allowlist: set[str]) -> Iterator[str]:
    with path.open("rb") as stream:
        if b"\0" in stream.read(8192):
            return
    with path.open(encoding="utf-8", errors="replace") as text:
        for number, line in enumerate(text, start=1):
            for kind, value in findings(line):
                if value not in allowlist:
                    yield f"{path}:{number}: {kind} {value[:2]}***"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", dest="all_files")
    args = parser.parse_args(argv)
    if args.all_files and args.files:
        parser.error("choose FILE... or --all")
    if not args.all_files and not args.files:
        parser.error("provide FILE... or --all")
    paths: list[Path] = args.files
    if args.all_files:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        ).stdout
        paths = [ROOT / name.decode() for name in tracked.split(b"\0") if name]
    allowlist = load_allowlist(ROOT / ".pii-allowlist")
    found = False
    failed = False
    for path in paths:
        try:
            for report in scan_file(path, allowlist):
                print(report)
                found = True
        except OSError as error:
            print(f"{path}: cannot read ({error.strerror})", file=sys.stderr)
            if not (args.all_files and isinstance(error, FileNotFoundError)):
                failed = True
    return 2 if failed else int(found)


if __name__ == "__main__":
    raise SystemExit(main())
