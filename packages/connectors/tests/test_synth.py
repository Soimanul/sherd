import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from itertools import islice
from pathlib import Path
from typing import Any

import pytest
from sherd_connectors import synth
from sherd_connectors.synth import generator
from sherd_connectors.synth.__main__ import main
from sherd_core import Store

from scripts import pii_scan

LOCAL = "AT TIME ZONE 'Europe/Bucharest'"


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    path = tmp_path_factory.mktemp("synth") / "demo.duckdb"
    with Store.open(path) as store:
        synth.import_profile(store, "demo")
        yield store


def rows(store: Store, sql: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = store.query(sql).to_pylist()
    return result


def scalar(store: Store, sql: str) -> Any:
    return store.query(sql).column(0)[0].as_py()


def child_fingerprint(profile: str, limit: int | None, seed: int = 42) -> str:
    code = (
        "import sys, itertools\n"
        "from sherd_connectors.synth import fingerprint, generate\n"
        f"rows = generate({profile!r}, {seed})\n"
        f"print(fingerprint(itertools.islice(rows, {limit})))\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    return done.stdout.strip()


# -- determinism -----------------------------------------------------------------------------


def test_demo_is_deterministic_across_processes() -> None:
    here = synth.fingerprint(synth.generate("demo", 42))
    assert here == synth.fingerprint(synth.generate("demo", 42))
    assert here == child_fingerprint("demo", None)
    assert here != synth.fingerprint(synth.generate("demo", 43))


def test_bench_first_rows_are_deterministic() -> None:
    first = synth.fingerprint(islice(synth.generate("bench"), 10_000))
    assert first == child_fingerprint("bench", 10_000)
    assert first != synth.fingerprint(islice(synth.generate("demo"), 10_000))


def test_bench_scale_factor_is_documented() -> None:
    assert synth.PROFILES == {"demo": 1, "bench": 40}
    assert 'PROFILES["bench"]` = 40' in (generator.__doc__ or "")


def test_unknown_profile() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        synth.generate("huge")  # type: ignore[arg-type]  # deliberately invalid


# -- shape -----------------------------------------------------------------------------------


def test_demo_size_tables_and_span(demo_db: Store) -> None:
    counts = demo_db.table_counts()
    assert 40_000 <= sum(counts.values()) <= 60_000
    assert {table for table, n in counts.items() if n} == {
        "messages",
        "media_plays",
        "transactions",
        "events",
    }
    for table in ("messages", "media_plays", "transactions", "events"):
        first, last = rows(demo_db, f"SELECT min(ts) AS a, max(ts) AS b FROM {table}")[0].values()
        assert generator.START_UTC <= first < generator.START_UTC + timedelta(days=30)
        assert generator.END_UTC - timedelta(days=30) < last < generator.END_UTC


def test_provenance(demo_db: Store) -> None:
    sources = rows(
        demo_db,
        "SELECT 'messages' AS t, source FROM messages UNION SELECT 'media_plays', source"
        " FROM media_plays UNION SELECT 'transactions', source FROM transactions"
        " UNION SELECT kind, source FROM events ORDER BY 1",
    )
    assert {(r["t"], r["source"]) for r in sources} == {
        ("messages", "whatsapp"),
        ("media_plays", "spotify"),
        ("transactions", "bank_csv"),
        ("youtube.watch", "google_takeout"),
        ("google.search", "google_takeout"),
        ("chrome.visit", "google_takeout"),
        ("shell.command", "shell_history"),
    }
    for table in ("messages", "media_plays", "transactions", "events"):
        assert rows(
            demo_db,
            f"SELECT DISTINCT source_file, left(source_row_id, 6) AS p FROM {table}",
        ) == [{"source_file": "synthetic/demo", "p": "synth:"}]
    [ledger] = rows(demo_db, "SELECT connector, tz, status, rows_inserted FROM imports")
    assert ledger["connector"] == "synth"
    assert ledger["tz"] == "Europe/Bucharest"
    assert ledger["status"] == "succeeded"
    assert ledger["rows_inserted"] == sum(demo_db.table_counts().values())


# -- realism the digs depend on ----------------------------------------------------------------


def test_contacts_are_fictional_and_reserved(demo_db: Store) -> None:
    senders = rows(demo_db, "SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL")
    numbers = {r["sender_id"].removeprefix("whatsapp:") for r in senders}
    assert 20 <= len(numbers) <= 30
    assert all(pii_scan.allowed_phone(number) for number in numbers)


def test_is_from_me_ratio(demo_db: Store) -> None:
    ratio = scalar(demo_db, "SELECT avg(is_from_me::INT) FROM messages WHERE kind <> 'system'")
    assert 0.4 <= ratio <= 0.6


def test_direct_and_group_chats_with_all_message_kinds(demo_db: Store) -> None:
    kinds = rows(demo_db, "SELECT chat_kind, kind, count(*) AS n FROM messages GROUP BY ALL")
    assert {(r["chat_kind"], r["kind"]) for r in kinds} >= {
        ("direct", "text"),
        ("direct", "media"),
        ("direct", "deleted"),
        ("group", "text"),
        ("group", "system"),
    }
    assert scalar(demo_db, "SELECT count(*) FROM messages WHERE reply_to_id IS NOT NULL") > 0
    emoji = scalar(demo_db, "SELECT count(*) FROM messages WHERE regexp_matches(text, '[😂🎉☕]')")
    assert emoji > 100


def test_night_owl_contact(demo_db: Store) -> None:
    shares = rows(
        demo_db,
        f"SELECT chat_name, avg((hour(ts {LOCAL}) >= 23 OR hour(ts {LOCAL}) < 4)::INT) AS night"
        " FROM messages WHERE chat_kind = 'direct' GROUP BY 1 HAVING count(*) >= 200"
        " ORDER BY night DESC",
    )
    assert shares[0]["chat_name"] == "Lavinia Owlcroft"
    assert shares[0]["night"] > 0.4
    assert all(r["night"] < 0.25 for r in shares[1:])


def test_reply_delays(demo_db: Store) -> None:
    median = scalar(
        demo_db,
        "SELECT median(epoch(ts) - epoch(prev)) FROM (SELECT ts, is_from_me,"
        " lag(ts) OVER w AS prev, lag(is_from_me) OVER w AS prev_me FROM messages"
        " WHERE chat_kind = 'direct' WINDOW w AS (PARTITION BY chat_id ORDER BY ts))"
        " WHERE prev_me <> is_from_me AND epoch(ts) - epoch(prev) < 6 * 3600",
    )
    assert 60 < median < 3600


def test_circle_changes_with_streaks_and_silences(demo_db: Store) -> None:
    def chats(year: int) -> set[str]:
        found = rows(
            demo_db, f"SELECT DISTINCT chat_name FROM messages WHERE year(ts {LOCAL}) = {year}"
        )
        return {r["chat_name"] for r in found}

    early, late = chats(2024), chats(2026)
    assert early - late  # people who left the circle
    assert late - early  # people who joined it
    gaps = rows(
        demo_db,
        "SELECT chat_name, max(gap) AS longest FROM (SELECT chat_name,"
        " date_diff('day', lag(ts) OVER (PARTITION BY chat_id ORDER BY ts), ts) AS gap"
        " FROM messages WHERE chat_name IN ('Mira Quillfeather', 'Lavinia Owlcroft', 'Mama'))"
        " GROUP BY 1",
    )
    assert all(r["longest"] >= 10 for r in gaps)
    busiest_month = scalar(
        demo_db,
        "SELECT max(n) / avg(n) FROM (SELECT date_trunc('month', ts) AS m, count(*) AS n"
        " FROM messages WHERE chat_name = 'Mira Quillfeather' GROUP BY 1)",
    )
    assert busiest_month > 1.5


def test_diurnal_and_weekday_rhythm(demo_db: Store) -> None:
    by_hour = {
        r["h"]: r["n"]
        for r in rows(
            demo_db, f"SELECT hour(ts {LOCAL}) AS h, count(*) AS n FROM messages GROUP BY 1"
        )
    }
    assert by_hour[20] > 5 * by_hour[4]
    weekend = scalar(
        demo_db,
        f"SELECT avg(n) FILTER (WHERE d IN (0, 6)) / avg(n) FILTER (WHERE d IN (1, 2, 3))"
        f" FROM (SELECT dayofweek(ts {LOCAL}) AS d, count(*) AS n FROM messages"
        f" GROUP BY date_trunc('day', ts {LOCAL}), d)",
    )
    assert weekend > 1.1


def test_monthly_subscriptions_and_salary(demo_db: Store) -> None:
    recurring = rows(
        demo_db,
        "SELECT merchant FROM transactions WHERE merchant IS NOT NULL AND amount < 0"
        " GROUP BY merchant HAVING count(DISTINCT strftime(ts, '%Y-%m')) >= 12"
        " AND count(*) = count(DISTINCT strftime(ts, '%Y-%m')) AND count(DISTINCT amount) <= 2",
    )
    merchants = {r["merchant"] for r in recurring}
    assert len(merchants) >= 4
    assert {"Netflix", "Spotify", "iCloud"} <= merchants
    salaries = scalar(
        demo_db,
        "SELECT count(DISTINCT strftime(ts, '%Y-%m')) FROM transactions"
        " WHERE category = 'salary' AND amount > 0",
    )
    assert salaries == 36
    currencies = rows(demo_db, "SELECT DISTINCT currency FROM transactions ORDER BY 1")
    assert [r["currency"] for r in currencies] == ["EUR", "RON"]


def test_spending_patterns(demo_db: Store) -> None:
    categories = rows(demo_db, "SELECT DISTINCT category FROM transactions")
    assert {"food_delivery", "subscriptions", "transfers", "groceries"} <= {
        r["category"] for r in categories
    }
    transfers = scalar(
        demo_db,
        "SELECT count(DISTINCT counterparty) FROM transactions WHERE category = 'transfers'",
    )
    assert transfers >= 10
    bump = scalar(
        demo_db,
        "SELECT avg(spent) FILTER (WHERE d IN (0, 5, 6))"
        " / avg(spent) FILTER (WHERE d IN (1, 2, 3))"
        f" FROM (SELECT dayofweek(ts {LOCAL}) AS d, sum(-amount) AS spent FROM transactions"
        f" WHERE kind = 'card' AND currency = 'RON' GROUP BY date_trunc('day', ts {LOCAL}), d)",
    )
    assert bump > 1.3


def test_obsession_week(demo_db: Store) -> None:
    weeks = rows(
        demo_db,
        f"WITH w AS (SELECT date_trunc('week', ts {LOCAL}) AS wk, track, count(*) AS n"
        " FROM media_plays WHERE media_kind = 'track' GROUP BY ALL)"
        " SELECT wk, arg_max(track, n) AS track, max(n) / sum(n) AS share FROM w"
        " GROUP BY wk ORDER BY share DESC LIMIT 2",
    )
    assert weeks[0]["track"] == "Paper Lanterns"
    assert str(weeks[0]["wk"]).startswith("2025-03-10")
    assert weeks[0]["share"] > 0.3
    assert weeks[1]["share"] < 0.15


def test_listening_differs_by_time_and_season(demo_db: Store) -> None:
    def top(where: str) -> str:
        result: str = scalar(
            demo_db,
            f"SELECT artist FROM media_plays WHERE media_kind = 'track' AND {where}"
            " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1",
        )
        return result

    assert top(f"hour(ts {LOCAL}) BETWEEN 7 AND 9") != top(f"hour(ts {LOCAL}) IN (23, 0, 1)")
    assert top("month(ts) IN (6, 7, 8)") != top("month(ts) IN (12, 1, 2)")
    skipped = scalar(demo_db, "SELECT avg(skipped::INT) FROM media_plays")
    assert 0.1 < skipped < 0.35


def test_event_kinds(demo_db: Store) -> None:
    kinds = rows(demo_db, "SELECT kind, count(*) AS n FROM events GROUP BY 1")
    assert {r["kind"] for r in kinds} == {
        "youtube.watch",
        "google.search",
        "chrome.visit",
        "shell.command",
    }
    assert scalar(demo_db, "SELECT count(*) FROM locations") == 0


def test_generated_values_pass_the_pii_scanner(tmp_path: Path) -> None:
    dump = tmp_path / "demo.jsonl"
    with dump.open("w", encoding="utf-8") as out:
        for row in synth.generate("demo"):
            out.write(json.dumps({"table": row.table_name, **row.model_dump(mode="json")}) + "\n")
    assert pii_scan.main([str(dump)]) == 0


# -- command line ------------------------------------------------------------------------------


def test_module_command_imports_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.duckdb"
    assert main(["--profile", "demo", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "messages" in out
    assert " s\n" in out
    with Store.open(db, read_only=True) as store:
        assert sum(store.table_counts().values()) > 40_000
