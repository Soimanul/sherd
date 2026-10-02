"""Deterministic raw WhatsApp exports for locale and memory checks."""

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

Locale = Literal["en-US", "en-GB", "ro-RO", "de-DE"]


def format_timestamp(ts: datetime, locale: Locale, *, ios: bool = False) -> str:
    if locale == "en-US":
        date = f"{ts.month}/{ts.day}/{ts.year % 100:02}"
        clock = f"{ts.hour % 12 or 12}:{ts.minute:02}"
        if ios:
            clock += f":{ts.second:02}"
        clock += " AM" if ts.hour < 12 else " PM"
    else:
        date = ts.strftime({"en-GB": "%d/%m/%Y", "ro-RO": "%d.%m.%Y", "de-DE": "%d.%m.%y"}[locale])
        clock = ts.strftime("%H:%M:%S" if ios else "%H:%M")
    return f"[{date}, {clock}]" if ios else f"{date}, {clock} -"


class WhatsAppGenerator:
    def __init__(self, locale: Locale = "en-US", *, ios: bool = False) -> None:
        self.locale = locale
        self.ios = ios

    def write(self, path: Path, approx_bytes: int, seed: int) -> None:
        rng = random.Random(seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Short distinct records expose accidental history-sized identity bookkeeping.
        padding = "Synthetic conversation about imaginary paper moons. " * 2
        start = datetime(2024, 1, 13, 8)
        written = index = 0
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            while written < approx_bytes:
                ts = start + timedelta(seconds=index if self.ios else index * 60)
                sender = "Alex Demo" if index % 2 else "Mira Example"
                line = (
                    f"{format_timestamp(ts, self.locale, ios=self.ios)} {sender}: "
                    f"{padding}{index} {rng.randrange(1000)} 🌙\n"
                )
                stream.write(line)
                written += len(line.encode("utf-8"))
                index += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--bytes", type=int, default=50 * 1024**2)
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--locale", choices=("en-US", "en-GB", "ro-RO", "de-DE"), default="en-US")
    parser.add_argument("--ios", action="store_true")
    args = parser.parse_args()
    WhatsAppGenerator(args.locale, ios=args.ios).write(args.path, args.bytes, args.seed)


if __name__ == "__main__":
    main()
