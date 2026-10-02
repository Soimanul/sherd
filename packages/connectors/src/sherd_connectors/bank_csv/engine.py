"""Streaming CSV import with bounded-memory occurrence counting."""

import csv
import dbm
import io
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import IO, TextIO
from zoneinfo import ZoneInfo

from sherd_core import Transaction, content_hash

from sherd_connectors.bank_csv.mapping import Mapping
from sherd_connectors.base import ImportContext


def normalise_merchant(raw: str) -> str:
    value = raw.lower()
    value = re.sub(r"https?://\S+|\b(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", " ", value)
    value = re.sub(r"\*\S+|#\d+|\d{4,}", " ", value)
    value = re.sub(r"\b(?:payment|purchase|card|pos)\b", " ", value)
    value = re.sub(r"(?:\s+[a-z]{2,3})+$", "", value.strip())
    value = " ".join(value.split()).title()
    return value or raw


def localise(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    aware = value.replace(tzinfo=tz, fold=0)
    # Round-tripping fold=0 shifts nonexistent wall times forward by the DST gap.
    return aware.astimezone(UTC).astimezone(tz).astimezone(UTC)


def files(path: Path) -> Iterator[Path]:
    if path.is_dir():
        yield from sorted(p for p in path.rglob("*") if p.suffix.lower() in {".csv", ".zip"})
    elif path.is_file():
        yield path


@contextmanager
def streams(path: Path) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:

            def members() -> Iterator[tuple[str, IO[bytes]]]:
                for member in archive.infolist():
                    if not member.is_dir() and member.filename.lower().endswith(".csv"):
                        with archive.open(member) as source:
                            yield member.filename, source

            yield members()
    else:
        with path.open("rb") as source:
            yield iter([(path.name, source)])


def reader(text: TextIO, mapping: Mapping) -> csv.DictReader[str]:
    for _ in range(mapping.csv.skip_rows):
        next(text, "")
    return csv.DictReader(text, delimiter=mapping.csv.delimiter)


def matches(headers: Sequence[str] | None, mapping: Mapping) -> bool:
    actual = {h.strip().casefold() for h in headers or []}
    return all(h.strip().casefold() in actual for h in mapping.detect.required_headers)


def choose(sample: bytes, mappings: list[Mapping]) -> Mapping | None:
    for mapping in mappings:
        try:
            text = io.StringIO(sample.decode(mapping.csv.encoding, errors="replace"))
            if matches(reader(text, mapping).fieldnames, mapping):
                return mapping
        except csv.Error:
            continue
    return None


def _get(row: dict[str, str], column: str | None) -> str:
    return row[column.strip().casefold()].strip() if column else ""


def _money(value: str, mapping: Mapping) -> Decimal:
    if not value:
        return Decimal("0.00")
    if mapping.csv.thousands:
        value = value.replace(mapping.csv.thousands, "")
    return Decimal(value.replace(mapping.csv.decimal, ".")).quantize(Decimal("0.01"))


def transaction(
    row: dict[str, str], mapping: Mapping, ctx: ImportContext, source_file: str
) -> Transaction | None:
    if mapping.include and _get(row, mapping.include.column) not in mapping.include.allowed:
        return None
    if mapping.amount.column and not _get(row, mapping.amount.column):
        raise ValueError("missing amount")
    if not (mapping.currency.fixed or _get(row, mapping.currency.column)):
        raise ValueError("missing currency")
    date = _get(row, mapping.ts.column) or _get(row, mapping.ts.fallback_column)
    ts = localise(datetime.strptime(date, mapping.ts.format), ctx.tz)
    amount = mapping.amount
    value = (
        _money(_get(row, amount.column), mapping)
        if amount.column
        else _money(_get(row, amount.credit), mapping) - _money(_get(row, amount.debit), mapping)
    )
    if amount.sign == "invert":
        value = -value
    value -= _money(_get(row, amount.fee), mapping)
    raw = _get(row, mapping.description)
    party: str | None = None
    if mapping.counterparty:
        if mapping.counterparty.column:
            party = _get(row, mapping.counterparty.column) or None
        elif mapping.counterparty.regex:
            match = re.search(mapping.counterparty.regex, raw)
            party = match.group("name").strip() if match else None
    account = "".join(
        literal + (_get(row, field) if field is not None else "")
        for literal, field, _, _ in Formatter().parse(mapping.account)
    )
    return Transaction(
        source_file=source_file,
        source_row_id="pending",
        ts=ts,
        amount=value,
        currency=mapping.currency.fixed or _get(row, mapping.currency.column),
        merchant_raw=raw,
        merchant=normalise_merchant(raw),
        counterparty=party,
        category=_get(row, mapping.category) or None,
        account=account,
        balance=_money(_get(row, mapping.balance), mapping) if _get(row, mapping.balance) else None,
        kind=mapping.kind.map.get(_get(row, mapping.kind.column), mapping.kind.default),
    )


def parse(path: Path, ctx: ImportContext, mappings: list[Mapping]) -> Iterator[Transaction]:
    skipped = 0
    for file in files(path):
        with streams(file) as sources:
            for name, binary in sources:
                mapping = choose(binary.read(65536), mappings)
                if mapping is None:
                    continue
                binary.seek(0)
                with io.TextIOWrapper(binary, encoding=mapping.csv.encoding, newline="") as text:
                    rows = reader(text, mapping)
                    relative = file.relative_to(ctx.export_root).as_posix()
                    source_file = (
                        f"{relative}/{name}" if file.suffix.lower() == ".zip" else relative
                    )
                    with (
                        tempfile.TemporaryDirectory(prefix="sherd-bank-") as temp,
                        dbm.open(str(Path(temp) / "occurrences"), "n") as counts,
                    ):
                        for original in rows:
                            first = next(iter(original.values()), "")
                            if mapping.csv.skip_footer_matching and re.search(
                                mapping.csv.skip_footer_matching, first or ""
                            ):
                                break
                            if None in original or any(v is None for v in original.values()):
                                skipped += 1
                                continue
                            row = {k.strip().casefold(): v for k, v in original.items()}
                            try:
                                result = transaction(row, mapping, ctx, source_file)
                            except (ValueError, KeyError, ArithmeticError):
                                skipped += 1
                                continue
                            if result is None:
                                continue
                            fields = (
                                mapping.id,
                                result.account,
                                result.ts.isoformat(),
                                result.amount,
                                result.currency,
                                result.merchant_raw,
                                result.balance,
                            )
                            key = content_hash(*fields).encode()
                            occurrence = int(counts.get(key, b"0"))
                            counts[key] = str(occurrence + 1).encode()
                            yield result.model_copy(
                                update={"source_row_id": content_hash(*fields, occurrence)}
                            )
    if skipped:
        logging.getLogger("sherd.connectors.bank_csv").warning(
            "bank_csv skipped malformed records: count=%d", skipped
        )
