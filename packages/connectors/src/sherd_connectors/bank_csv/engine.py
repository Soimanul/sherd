"""Streaming CSV import with bounded-memory occurrence counting."""

import csv
import io
import logging
import re
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from string import Formatter
from typing import IO, TextIO
from zoneinfo import ZoneInfo

from sherd_core import Transaction, content_hash

from sherd_connectors.bank_csv.mapping import Mapping, MappingError
from sherd_connectors.base import ImportContext

# Country codes for common statement markets; city abbreviations used in card
# descriptors: Bucharest, London, New York and Singapore.
_LOCATION_CODES = frozenset(
    [
        "RO",
        "ROU",
        "GB",
        "GBR",
        "UK",
        "US",
        "USA",
        "DE",
        "DEU",
        "FR",
        "FRA",
        "IT",
        "ITA",
        "ES",
        "ESP",
        "NL",
        "NLD",
        "IE",
        "IRL",
        "CH",
        "CHE",
        "AT",
        "AUT",
        "CA",
        "CAN",
        "AU",
        "AUS",
        "SG",
        "SGP",
        "BUC",
        "LDN",
        "NYC",
        "SIN",
    ]
) - {"UK"}  # UK is not an ISO 3166 alpha-2 code.


def normalise_merchant(raw: str) -> str:
    value = raw
    value = re.sub(r"https?://\S+|\b(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", " ", value, flags=re.I)
    value = re.sub(r"\*\S+|#\d+|\d{4,}", " ", value)
    value = re.sub(r"\b(?:payment|purchase|card|pos)\b", " ", value, flags=re.I)
    tokens = value.split()
    if len(tokens) > 1 and tokens[-1] in _LOCATION_CODES:
        tokens.pop()
    value = " ".join(tokens)
    if not any(c.isalpha() for c in value):
        return raw
    # Title case words without treating apostrophes as word boundaries.
    return re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", lambda m: m[0].capitalize(), value)


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


def _money(value: str, mapping: Mapping, rounded: list[bool]) -> Decimal:
    if not value:
        return Decimal("0.00")
    if mapping.csv.thousands:
        value = value.replace(mapping.csv.thousands, "")
    number = Decimal(value.replace(mapping.csv.decimal, "."))
    result = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        rounded[0] = True
    return result


def transaction(
    row: dict[str, str], mapping: Mapping, ctx: ImportContext, source_file: str, rounded: list[bool]
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
        _money(_get(row, amount.column), mapping, rounded)
        if amount.column
        else _money(_get(row, amount.credit), mapping, rounded)
        - _money(_get(row, amount.debit), mapping, rounded)
    )
    if amount.sign == "invert":
        value = -value
    value -= _money(_get(row, amount.fee), mapping, rounded)
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
        balance=_money(_get(row, mapping.balance), mapping, rounded)
        if _get(row, mapping.balance)
        else None,
        kind=mapping.kind.map.get(_get(row, mapping.kind.column), mapping.kind.default),
    )


def validate_headers(headers: Sequence[str] | None, mapping: Mapping) -> None:
    actual = {h.strip().casefold() for h in headers or []}
    references: dict[str, str | None] = {
        "description": mapping.description,
        "balance": mapping.balance,
        "category": mapping.category,
    }
    for field in ("ts", "amount", "currency", "counterparty", "kind", "include"):
        model = getattr(mapping, field)
        if model is not None:
            for key, column in model.model_dump().items():
                if key in {"column", "fallback_column", "debit", "credit", "fee"}:
                    references[f"{field}.{key}"] = column
    for _, column, _, _ in Formatter().parse(mapping.account):
        if column is not None:
            references[f"account.{column}"] = column
    for field, column in references.items():
        if column and column.strip().casefold() not in actual:
            raise MappingError(f"mapping {mapping.id}: field {field}: missing column {column}")


def parse(path: Path, ctx: ImportContext, mappings: list[Mapping]) -> Iterator[Transaction]:
    skipped = rounded_rows = 0
    for file in files(path):
        with streams(file) as sources:
            for name, binary in sources:
                mapping = choose(binary.read(65536), mappings)
                if mapping is None:
                    continue
                binary.seek(0)
                relative = file.relative_to(ctx.export_root).as_posix()
                source_file = f"{relative}/{name}" if file.suffix.lower() == ".zip" else relative
                try:
                    with io.TextIOWrapper(
                        binary, encoding=mapping.csv.encoding, newline=""
                    ) as text:
                        rows = reader(text, mapping)
                        validate_headers(rows.fieldnames, mapping)
                        counts: dict[str, int] = {}
                        current_ts: datetime | None = None
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
                            rounded = [False]
                            try:
                                result = transaction(row, mapping, ctx, source_file, rounded)
                            except (ValueError, KeyError, ArithmeticError):
                                skipped += 1
                                continue
                            if result is None:
                                continue
                            rounded_rows += int(rounded[0])
                            if result.ts != current_ts:
                                counts.clear()
                                current_ts = result.ts
                            fields = (
                                mapping.id,
                                result.account,
                                result.ts.isoformat(),
                                result.amount,
                                result.currency,
                                result.merchant_raw,
                                result.balance,
                            )
                            key = content_hash(*fields)
                            occurrence = counts.get(key, 0)
                            counts[key] = occurrence + 1
                            yield result.model_copy(
                                update={"source_row_id": content_hash(*fields, occurrence)}
                            )
                except UnicodeDecodeError:
                    raise MappingError(
                        f"{source_file}: cannot decode CSV; "
                        f"check mapping {mapping.id} encoding setting"
                    ) from None
    logger = logging.getLogger("sherd.connectors.bank_csv")
    if skipped:
        logger.warning("bank_csv skipped malformed records: count=%d", skipped)
    if rounded_rows:
        logger.warning("bank_csv rounded records: count=%d", rounded_rows)
