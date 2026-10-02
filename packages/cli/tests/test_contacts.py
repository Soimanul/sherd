from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sherd_cli.commands import dig
from sherd_cli.main import create_app
from sherd_core import Message, Row, Store, Transaction
from sherd_core.entities import resolve
from typer.testing import CliRunner


def seed(store: Store) -> None:
    stamp = datetime(2025, 1, 1, tzinfo=UTC)
    batches: list[tuple[str, list[Row]]] = [
        (
            "whatsapp",
            [
                Message(
                    source_file="synthetic",
                    source_row_id="a",
                    ts=stamp,
                    chat_id="direct",
                    chat_kind="direct",
                    sender_id="whatsapp:+15550101",
                    sender_name="Ana",
                    is_from_me=False,
                    kind="text",
                )
            ],
        ),
        (
            "bank_csv",
            [
                Transaction(
                    source_file="synthetic",
                    source_row_id="b",
                    ts=stamp,
                    amount=Decimal("-10"),
                    currency="RON",
                    account="demo",
                    kind="transfer",
                    counterparty="Ana Pop",
                )
            ],
        ),
    ]
    for source, rows in batches:
        imp = store.begin_import(source, "1", "synthetic", "UTC")
        store.upsert(imp, source, rows)
        store.finish_import(imp, "succeeded")


def invoke(path: Path, *args: str) -> str:
    result = CliRunner().invoke(create_app(), ["contacts", "--db", str(path), *args])
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


def test_list_proposals_reject_merge_resolve(tmp_path: Path) -> None:
    path = tmp_path / "contacts.duckdb"
    with Store.open(path) as store:
        seed(store)
    assert "1 proposals" in invoke(path, "resolve")
    assert "Ana" in invoke(path)
    assert "0.70" in invoke(path, "proposals")
    assert "0 proposals" in invoke(path, "reject", "whatsapp:+15550101", "bank:name:ana pop")
    assert "0 proposals" in invoke(path, "proposals")
    with Store.open(path) as store:
        ids = [r["id"] for r in store.query("SELECT id FROM contacts").to_pylist()]
    assert "0 proposals" in invoke(path, "merge", *ids)
    assert "0 automatic merges" in invoke(path, "resolve")
    with Store.open(path) as store:
        assert store.query(
            "SELECT count(*) AS n FROM contacts WHERE merged_into IS NULL"
        ).to_pylist() == [{"n": 1}]


def test_demo_and_invalid_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path))
    result = CliRunner().invoke(create_app(), ["contacts", "--demo", "resolve"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "demo.duckdb").exists()
    conflict = CliRunner().invoke(create_app(), ["contacts", "--db", str(tmp_path / "x"), "--demo"])
    assert conflict.exit_code == 2
    unknown = CliRunner().invoke(create_app(), ["contacts", "--demo", "merge", "missing", "other"])
    assert unknown.exit_code == 1
    assert "unknown" in unknown.output


def test_successful_dig_resolves_and_failed_dig_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHERD_HOME", str(tmp_path / "home"))
    export = tmp_path / "WhatsApp Chat with Ana Pop.txt"
    export.write_text(
        "01/01/2025, 09:00 - Ana Pop: Synthetic greeting\n"
        "01/01/2025, 09:01 - Alex Demo: Synthetic reply\n"
    )
    calls: list[object] = []

    def spy(store: Store) -> object:
        calls.append(store)
        return resolve(store)

    monkeypatch.setattr(dig, "resolve", spy)
    result = CliRunner().invoke(
        create_app(),
        ["dig", str(export), "--connector", "whatsapp", "--tz", "UTC", "--me", "Alex Demo"],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    with Store.open(tmp_path / "home" / "life.duckdb") as store:
        assert store.query("SELECT count(contact_id) AS n FROM messages").to_pylist() == [{"n": 2}]

    def fail(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic parse failure")

    monkeypatch.setattr(dig, "run_import", fail)
    failed = CliRunner().invoke(
        create_app(), ["dig", str(export), "--connector", "whatsapp", "--tz", "UTC"]
    )
    assert failed.exit_code == 1
    assert len(calls) == 1


def test_message_digs_work_after_resolution(tmp_path: Path) -> None:
    from sherd_connectors import synth
    from sherd_insights import DigParams
    from sherd_insights.registry import discover

    with Store.open(tmp_path / "demo.duckdb") as store:
        synth.import_profile(store, "demo")
        resolve(store)
        digs = [d for d in discover().values() if d.id.startswith("messages.")]
        assert len(digs) >= 6
        for insight in digs:
            result = insight.compute(store, DigParams())
            assert result.data.num_rows > 0, insight.id
            assert result.text_summary
