"""Resolution acceptance tests, using only synthetic identities."""

from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

import duckdb
import pytest
from sherd_connectors import synth
from sherd_connectors.synth.data import CONTACTS
from sherd_core import Message, Row, Store, Transaction
from sherd_core.entities import decide, normalise_name, resolve, score
from sherd_core.schema import MIGRATIONS
from sherd_core.store import ReadOnlyStoreError


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Ștefan", "Stefan", 1.0),
        ("Ána", "ana", 1.0),
        ("Ana Pop", "Pop Ana", 0.95),
        ("ANA  POP", "Ana Pop", 1.0),
        ("Ana ❤️ Pop", "Ana Pop", 1.0),
        ("Ana-Pop", "Ana Pop", 1.0),
        ("Ana", "Ana Pop", 0.70),
        ("Pop", "Ana Pop", 0.70),
        ("Ana Pop", "Ana Maria Pop", 0.70),
        ("Bob", "Robert", 0.0),
        ("Liz", "Elizabeth", 0.0),
        ("Alex", "Alexander", 0.0),
        ("Ana Pop", "Ana Quillfeather", 0.0),
        ("Ana Pop", "Ana Moonvale", 0.0),
        ("Mira Quillfeather", "Mira Quillfeathe", 32 / 33),
        ("Mira Quillfeather", "Mira Quillfeathr", 32 / 33),
        ("", "", 0.0),
        ("❤️", "❤️", 0.0),
        ("Ana", "Radu", 0.0),
        ("+1 555 0101", "+15550101", 1.0),
        ("+15550101", "+15550102", 0.0),
        ("+15550101", "Ana", 0.0),
        ("Ilinca Brightwater", "Brightwater Ilinca", 0.95),
    ],
)
def test_scoring_table(a: str, b: str, expected: float) -> None:
    assert score(a, b) == pytest.approx(expected)
    assert score(b, a) == pytest.approx(expected)


def message(
    key: str,
    name: str | None,
    sender: str | None,
    *,
    me: bool = False,
    group: bool = False,
    chat: str = "direct",
) -> Message:
    return Message(
        source_file="synthetic",
        source_row_id=key,
        ts=datetime(2025, 1, 1, tzinfo=UTC),
        chat_id=chat,
        chat_name="Synthetic chat",
        chat_kind="group" if group else "direct",
        sender_name=name,
        sender_id=sender,
        is_from_me=me,
        kind="text" if sender else "system",
    )


def transfer(key: str, name: str, kind: Literal["transfer", "card"] = "transfer") -> Transaction:
    return Transaction(
        source_file="synthetic",
        source_row_id=key,
        ts=datetime(2025, 1, 1, tzinfo=UTC),
        amount=Decimal("-10"),
        currency="RON",
        account="demo",
        kind=kind,
        counterparty=name,
    )


def insert(store: Store, source: str, rows: list[Row]) -> None:
    imp = store.begin_import(source, "1", "synthetic", "UTC")
    store.upsert(imp, source, rows)
    store.finish_import(imp, "succeeded")


def snapshot(store: Store) -> list[dict[str, Any]]:
    return list(store.query("SELECT * FROM contacts ORDER BY id").to_pylist())


def test_assignments_aliases_idempotency_and_new_import(store: Store) -> None:
    insert(
        store,
        "whatsapp",
        [
            message("in", "Ana Pop", "whatsapp:+15550101"),
            message("out", "Alex Demo", "whatsapp:+15550100", me=True),
            message("system", None, None),
            message("gin", "Ána Pop", "whatsapp:+15550101", group=True, chat="group"),
            message("gin2", "Ana Pop", "whatsapp:+15550101", group=True, chat="group"),
            message("gout", "Alex Demo", "whatsapp:+15550100", me=True, group=True, chat="group"),
            message("gsys", None, None, group=True, chat="group"),
            message("empty", "Alex Demo", "whatsapp:+15550100", me=True, chat="only-self"),
        ],
    )
    insert(store, "bank_csv", [transfer("t", "Pop Ana"), transfer("card", "Ana Pop", "card")])
    report = resolve(store)
    assert report.auto_merged == 1
    assert report.assigned_messages == 5
    assert report.assigned_transactions == 1
    rows = store.query("SELECT source_row_id, contact_id FROM messages").to_pylist()
    contacts = {r["source_row_id"]: r["contact_id"] for r in rows}
    cid = contacts["in"]
    assert cid
    assert {contacts[k] for k in ("out", "system", "gin", "gin2")} == {cid}
    assert all(contacts[k] is None for k in ("gout", "gsys", "empty"))
    assert (
        store.query("SELECT contact_id FROM transactions WHERE source_row_id='t'").to_pylist()[0][
            "contact_id"
        ]
        == cid
    )
    assert (
        store.query("SELECT contact_id FROM transactions WHERE source_row_id='card'").to_pylist()[
            0
        ]["contact_id"]
        is None
    )
    before = snapshot(store)
    assert before[0]["display_name"] == "Ana Pop"
    assert before[0]["aliases"] == ["Ana Pop", "Pop Ana", "Ána Pop"]
    again = resolve(store)
    assert (again.auto_merged, again.assigned_messages, again.assigned_transactions) == (0, 0, 0)
    assert snapshot(store) == before
    insert(store, "whatsapp", [message("new", "Ana Pop", "whatsapp:+15550101")])
    resolve(store)
    assert snapshot(store) == before


def test_proposals_reject_persists_and_confirmed_merge(store: Store) -> None:
    insert(store, "whatsapp", [message("a", "Ana", "whatsapp:+15550101")])
    insert(store, "bank_csv", [transfer("b", "Ana Pop")])
    (proposal,) = resolve(store).proposals
    assert proposal.score == 0.70
    assert len(snapshot(store)) == 2
    decide(store, proposal.b, proposal.a, "reject")
    assert not resolve(store).proposals
    assert len(snapshot(store)) == 2
    assert store.query("SELECT decision FROM contact_decisions").to_pylist() == [
        {"decision": "reject"}
    ]
    decide(store, proposal.a, proposal.b, "merge")
    assert not resolve(store).proposals
    active = [r for r in snapshot(store) if r["merged_into"] is None]
    assert len(active) == 1
    assert resolve(store).auto_merged == 0
    assert store.query(
        "SELECT count(DISTINCT contact_id) AS n FROM "
        "(SELECT contact_id FROM messages UNION ALL SELECT contact_id FROM transactions)"
    ).to_pylist() == [{"n": 1}]


def test_conflicting_phones_and_transitive_bridge_never_auto_merge(store: Store) -> None:
    insert(
        store,
        "whatsapp",
        [
            message("a", "Ana Pop", "whatsapp:+15550101", chat="a"),
            message("b", "Ana Pop", "whatsapp:+15550102", chat="b"),
        ],
    )
    insert(store, "bank_csv", [transfer("bridge", "Ana Pop")])
    report = resolve(store)
    assert len([r for r in snapshot(store) if r["merged_into"] is None]) == 2
    assert report.proposals
    decide(store, "whatsapp:+15550101", "whatsapp:+15550102", "merge")
    assert len([r for r in snapshot(store) if r["merged_into"] is None]) == 1


def test_rejection_blocks_indirect_merge(store: Store) -> None:
    insert(
        store,
        "whatsapp",
        [
            message("a", "Ana Pop", "whatsapp:name:Ana Pop", chat="a"),
            message("b", "Ana Pop", "whatsapp:name:Pop Ana", chat="b"),
        ],
    )
    store.decide_contacts("whatsapp:name:Ana Pop", "whatsapp:name:Pop Ana", "reject")
    insert(store, "bank_csv", [transfer("bridge", "Ana Pop")])
    resolve(store)
    rows = store.query("SELECT contact_id FROM messages ORDER BY source_row_id").to_pylist()
    assert rows[0]["contact_id"] != rows[1]["contact_id"]
    assert all(
        {p.a, p.b} != {"whatsapp:name:Ana Pop", "whatsapp:name:Pop Ana"}
        for p in resolve(store).proposals
    )


def test_v1_migration_preserves_facts_and_decisions(tmp_path: Path) -> None:
    path = tmp_path / "v1.duckdb"
    with duckdb.connect(str(path)) as conn:
        for sql in MIGRATIONS[1]:
            conn.execute(sql)
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
        conn.execute(
            """INSERT INTO messages
            (id, source, source_file, source_row_id, import_id, imported_at,
             chat_id, chat_kind, sender_id, sender_name, is_from_me, ts, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "old",
                "whatsapp",
                "synthetic",
                "old",
                "old-import",
                datetime.now(UTC),
                "chat",
                "direct",
                "whatsapp:+15550101",
                "Ana Pop",
                False,
                datetime.now(UTC),
                "text",
            ],
        )

    with Store.open(path) as store:
        assert store.query("SELECT value FROM schema_meta").to_pylist() == [{"value": "2"}]
        assert store.table_counts()["messages"] == 1
        resolve(store)
        assert store.query("SELECT contact_id FROM messages").to_pylist()[0]["contact_id"]
        store.decide_contacts("a", "b", "reject")
    with Store.open(path) as store:
        assert store.query("SELECT decision FROM contact_decisions").to_pylist() == [
            {"decision": "reject"}
        ]
    with Store.open(path, read_only=True) as store:
        with pytest.raises(ReadOnlyStoreError):
            store.replace_contacts([])
        with pytest.raises(ReadOnlyStoreError):
            store.assign_contacts({}, {})
        with pytest.raises(ReadOnlyStoreError):
            store.decide_contacts("a", "b", "merge")


def test_synth_precision_and_transfer_recall(store: Store) -> None:
    synth.import_profile(store, "demo")
    report = resolve(store)
    expected = {"whatsapp:" + c.phone: normalise_name(c.name) for c in CONTACTS}
    expected.update(
        {
            "bank:name:" + " ".join(sorted(normalise_name(c.name).split())): normalise_name(c.name)
            for c in CONTACTS
        }
    )
    merged = correct = 0
    for contact in snapshot(store):
        truth = [expected[k] for k in contact["identities"] if k in expected]
        for a, b in combinations(truth, 2):
            merged += 1
            correct += a == b
    assert merged > 0
    assert correct == merged
    wa = {k: c["id"] for c in snapshot(store) for k in c["identities"] if k.startswith("whatsapp:")}
    names = {c.name: wa["whatsapp:" + c.phone] for c in CONTACTS}
    transfers = store.query(
        "SELECT counterparty, contact_id FROM transactions WHERE kind='transfer'"
    ).to_pylist()
    eligible = [r for r in transfers if r["counterparty"] in names]
    linked = sum(r["contact_id"] == names[r["counterparty"]] for r in eligible)
    assert eligible
    assert linked / len(eligible) >= 0.90
    print(
        f"Auto-merge precision: {correct}/{merged}; transfer links: {linked}/{len(eligible)}; "
        f"auto merges: {report.auto_merged}"
    )


def test_fuzzy_proposal_and_unrelated_identity_prefixes(store: Store) -> None:
    insert(
        store,
        "whatsapp",
        [
            message("a", "Ana Pop", "whatsapp:name:Ana Pop", chat="a"),
            message("b", "Ana Quillfeather", "whatsapp:name:Ana Quillfeather", chat="b"),
            message("c", "Mira Quillfeather", "whatsapp:name:Mira Quillfeather", chat="c"),
        ],
    )
    insert(store, "bank_csv", [transfer("d", "Mira Quillfethr")])
    report = resolve(store)
    assert report.auto_merged == 0
    (proposal,) = report.proposals
    assert 0.85 <= proposal.score < 0.95
    assert len(snapshot(store)) == 4


def test_ids_survive_new_identity_and_ambiguous_direct_chat(store: Store) -> None:
    insert(store, "whatsapp", [message("a", "Ana Pop", "whatsapp:+15550101")])
    resolve(store)
    cid = snapshot(store)[0]["id"]
    insert(store, "whatsapp", [message("b", "Pop Ana", "whatsapp:name:Pop Ana", chat="another")])
    insert(store, "bank_csv", [transfer("t", "ANA POP")])
    resolve(store)
    assert [r["id"] for r in snapshot(store) if r["merged_into"] is None] == [cid]
    insert(
        store,
        "whatsapp",
        [
            message("other", "Radu Moonvale", "whatsapp:+15550102"),
            message("out", "Alex Demo", "whatsapp:+15550100", me=True),
        ],
    )
    resolve(store)
    assert all(
        r["contact_id"] is None
        for r in store.query("SELECT contact_id FROM messages WHERE chat_id='direct'").to_pylist()
    )


def test_replace_contacts_is_atomic(store: Store) -> None:
    insert(store, "whatsapp", [message("a", "Ana Pop", "whatsapp:+15550101")])
    resolve(store)
    before = snapshot(store)
    invalid = dict(before[0], display_name=None)
    with pytest.raises(duckdb.ConstraintException):
        store.replace_contacts([invalid])
    assert snapshot(store) == before
