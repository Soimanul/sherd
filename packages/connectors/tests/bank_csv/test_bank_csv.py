"""Mapping, provenance, streaming and import integration checks."""

import csv
import logging
import shutil
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from sherd_connectors.bank_csv import CONNECTOR
from sherd_connectors.bank_csv.engine import localise, normalise_merchant
from sherd_connectors.bank_csv.mapping import MappingError, load_mapping
from sherd_connectors.bank_csv.synth_bank_csv import GENERATOR, HEADERS
from sherd_connectors.base import ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_connectors.testing import assert_golden, assert_streaming, export_path, load_meta
from sherd_core import Store, content_hash

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "bank_csv"
MAPPINGS = Path(__file__).resolve().parents[2] / "src/sherd_connectors/bank_csv/mappings"


def ctx(path: Path, tz: str = "UTC") -> ImportContext:
    return ImportContext(export_root(path), ZoneInfo(tz), frozenset())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fictional Market *T123 RO", "Fictional Market"),
        ("Example Cafe #123", "Example Cafe"),
        ("Example Shop 123456", "Example Shop"),
        ("Example Shop https://example.com/pay", "Example Shop"),
        ("Example Shop example.com", "Example Shop"),
        ("Example Shop www.example.org/order", "Example Shop"),
        ("PAYMENT Example Market", "Example Market"),
        ("purchase Example Market", "Example Market"),
        ("Card Example Market", "Example Market"),
        ("POS Example Market", "Example Market"),
        ("   Example    Market   ", "Example Market"),
        ("example market RO", "Example Market"),
        ("example market BUC", "Example Market"),
        ("example market RO BUC", "Example Market"),
        ("example market *terminal #12 1234", "Example Market"),
        ("Example Market 123", "Example Market 123"),
        ("Example Market 12", "Example Market 12"),
        ("Example-market", "Example-Market"),
        ("Payment Card POS Purchase", "Payment Card POS Purchase"),
        ("*terminal", "*terminal"),
        ("", ""),
        ("example market shop", "Example Market Shop"),
        ("example market https://example.net #42 RO", "Example Market"),
    ],
)
def test_normalisation(raw: str, expected: str) -> None:
    assert normalise_merchant(raw) == expected


@pytest.mark.parametrize("variant", ["revolut-basic", "revolut-overlap", "example-bank"])
def test_detect(variant: str) -> None:
    path = export_path(FIXTURES / variant)
    assert CONNECTOR.detect(path).confidence == 0.95
    if path.is_dir():
        assert CONNECTOR.detect(next(path.glob("*.csv"))).confidence == 0.95


def test_detect_negative(tmp_path: Path) -> None:
    p = tmp_path / "other.csv"
    p.write_text("Date,Description,Amount\n2024-01-01,Example,10\n")
    assert CONNECTOR.detect(p).confidence == 0
    assert CONNECTOR.detect(tmp_path / "missing").confidence == 0
    for root in FIXTURES.parent.iterdir():
        if root.name != "bank_csv":
            for meta in root.glob("*/meta.json"):
                assert CONNECTOR.detect(export_path(meta.parent)).confidence == 0


def test_header_matching_and_zip(tmp_path: Path) -> None:
    original = FIXTURES / "revolut-basic/export/statement.csv"
    p = tmp_path / "mixed.csv"
    lines = original.read_text().splitlines(keepends=True)
    lines[0] = ",".join(" " + h.lower() + " " for h in HEADERS) + "\n"
    p.write_text("".join(lines))
    assert CONNECTOR.detect(p).confidence == 0.95
    expected = list(CONNECTOR.parse(original, ctx(original)))
    actual = list(CONNECTOR.parse(p, ctx(p)))
    assert [r.source_row_id for r in actual] == [r.source_row_id for r in expected]
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(p, "nested/statement.csv")
    assert CONNECTOR.detect(z).confidence == 0.95
    zipped = list(CONNECTOR.parse(z, ctx(z)))
    assert [r.source_row_id for r in zipped] == [r.source_row_id for r in expected]
    assert all(r.source_file == "export.zip/nested/statement.csv" for r in zipped)
    assert list(tmp_path.iterdir()) == [p, z]


def test_detect_bounded(tmp_path: Path) -> None:
    p = tmp_path / "large.csv"
    with p.open("wb") as out:
        out.write(b"not,bank\n" + b"x" * 100000)
    assert CONNECTOR.detect(p).confidence == 0


@pytest.mark.parametrize(
    ("field", "value", "error_field"),
    [
        ("amount", {"column": "Amount", "debit": "Debit"}, "amount"),
        ("currency", {"column": "Currency", "fixed": "RON"}, "currency"),
        ("counterparty", {"regex": "(.*)"}, "counterparty"),
        ("csv", {"decimal": ";"}, "csv.decimal"),
        ("csv", {"skip_rows": -1}, "csv.skip_rows"),
        ("csv", {"delimiter": ";;"}, "csv.delimiter"),
        ("kind", {"default": "bad"}, "kind.default"),
        ("account", "{Product!r}", "account"),
        ("unknown", True, "unknown"),
        ("csv", {"encoding": "invalid-codec"}, "csv.encoding"),
        ("csv", {"skip_footer_matching": "["}, "csv.skip_footer_matching"),
        ("counterparty", {"regex": "["}, "counterparty.regex"),
    ],
)
def test_mapping_validation(tmp_path: Path, field: str, value: Any, error_field: str) -> None:
    data = yaml.safe_load((MAPPINGS / "revolut.yaml").read_text())
    data[field] = value
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(MappingError, match=error_field) as raised:
        load_mapping(p)
    assert str(p) in str(raised.value)


def test_user_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = FIXTURES / "user-mapping"
    user = tmp_path / "bank_mappings"
    user.mkdir()
    shutil.copy(variant / "mapping.yaml", user / "custom.yaml")
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    assert CONNECTOR.detect(export_path(variant)).confidence == 0.95
    rows = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert len(rows) == 1
    assert rows[0].amount == Decimal("-12.00")
    assert rows[0].counterparty == "Mira Fiction"
    assert rows[0].account == "user:synthetic"
    assert_golden(CONNECTOR, variant)
    (user / "bad.yaml").write_text("id: broken\n")
    with pytest.raises(MappingError, match=r"bad.yaml.*ts"):
        CONNECTOR.detect(export_path(variant))


def test_duplicate_mapping_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "bank_mappings"
    user.mkdir()
    path = user / "duplicate.yaml"
    shutil.copy(MAPPINGS / "revolut.yaml", path)
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    with pytest.raises(MappingError, match=r"duplicate.yaml: invalid field id"):
        CONNECTOR.detect(export_path(FIXTURES / "revolut-basic"))


def test_revolut_fee_filter_kinds_identity() -> None:
    variant = FIXTURES / "revolut-basic"
    rows = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert len(rows) == 10
    assert rows[0].amount == Decimal("-26.00")
    assert [r.kind for r in rows] == [
        "card",
        "transfer",
        "transfer",
        "transfer",
        "topup",
        "fee",
        "exchange",
        "refund",
        "refund",
        "other",
    ]
    assert [r.counterparty for r in rows[1:4]] == ["Mira Fiction", "Dorian Example", "Luma Fiction"]
    assert rows[6].account == "revolut:Current:EUR"
    r = rows[0]
    assert r.source_row_id == content_hash(
        "revolut", r.account, r.ts.isoformat(), r.amount, r.currency, r.merchant_raw, r.balance, 0
    )
    assert rows[-1].ts == datetime(2024, 1, 10, 8, tzinfo=UTC)
    assert all(r.meta is None for r in rows)


def test_example_decimal_debit_credit_footer() -> None:
    variant = FIXTURES / "example-bank"
    rows = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert [r.amount for r in rows] == [Decimal("-25.50"), Decimal("1200.00")]
    assert [r.balance for r in rows] == [Decimal("1000.00"), Decimal("2200.00")]
    assert [r.category for r in rows] == ["groceries", "income"]
    assert all(r.currency == "RON" for r in rows)


def test_malformed_records_and_dst(caplog: pytest.LogCaptureFixture) -> None:
    variant = FIXTURES / "revolut-edge"
    with caplog.at_level(logging.WARNING):
        rows = list(CONNECTOR.parse(export_path(variant), load_meta(variant)))
    assert len(rows) == 2
    assert rows[0].ts == datetime(2024, 3, 31, 1, 30, tzinfo=UTC)
    assert rows[1].ts == datetime(2024, 10, 27, 0, 30, tzinfo=UTC)
    assert caplog.messages == ["bank_csv skipped malformed records: count=4"]
    assert localise(datetime(2024, 1, 1, tzinfo=UTC), ZoneInfo("Europe/Bucharest")).hour == 0


def test_overlap_occurrences_and_idempotency(tmp_path: Path) -> None:
    root = FIXTURES / "revolut-overlap/export"
    first, second = root / "first.csv", root / "second.csv"
    rows = list(CONNECTOR.parse(first, ctx(first)))
    assert len({r.source_row_id for r in rows}) == 3
    assert rows[1].source_row_id != rows[2].source_row_id
    store = Store.open(tmp_path / "life.duckdb")
    try:
        assert run_import(store, CONNECTOR, first, ctx(first)).inserted == 3
        assert run_import(store, CONNECTOR, first, ctx(first)).inserted == 0
        assert run_import(store, CONNECTOR, second, ctx(second)).inserted == 1
        assert run_import(store, CONNECTOR, root, ctx(root)).inserted == 0
        assert store.table_counts()["transactions"] == 4
    finally:
        store.close()


def test_quoted_newlines_and_malformed_shape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "test.csv"
    with p.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(HEADERS)
        writer.writerow(
            [
                "CARD_PAYMENT",
                "Current",
                "2024-01-01 12:00:00",
                "2024-01-01 12:00:00",
                "Example\nMarket",
                "-10",
                "0",
                "RON",
                "COMPLETED",
                "",
            ]
        )
        writer.writerow(["CARD_PAYMENT", "Current"])
    rows = list(CONNECTOR.parse(p, ctx(p)))
    assert len(rows) == 1
    assert rows[0].merchant_raw == "Example\nMarket"
    assert rows[0].balance is None
    assert "count=1" in caplog.text


def test_streaming(tmp_path: Path) -> None:
    p = tmp_path / "large.csv"
    GENERATOR.write(p, 50 * 1024 * 1024, 42)
    assert p.stat().st_size >= 50 * 1024 * 1024
    assert_streaming(CONNECTOR, p, max_rss_mb=200)
