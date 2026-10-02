"""WP-15 pages: Messages, Music, Money, Timeline and Settings, plus the checks every page shares."""

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sherd_agent import config
from sherd_core import Store
from sherd_core.models import Event, MediaPlay, Message, Transaction
from sherd_insights import DigParams, registry
from sherd_web import app as web_app
from sherd_web.deps import local_zone, t
from sherd_web.routes import _pages, ask

ClientFactory = Callable[..., TestClient]

PAGES = ["/messages", "/music", "/money", "/timeline", "/ask", "/settings"]
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
URL = re.compile(r"https?://[^\s\"'<>)`]+")
NAMESPACES = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}
SPEC = re.compile(r'<script type="application/json" id="([^"]+)">(.*?)</script>', re.DOTALL)
HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def sherd_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config, consent and the privacy counter live in a temporary $SHERD_HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SHERD_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # No Ollama probe on loopback: providers are whatever a test says.
    monkeypatch.setattr(ask, "PROBE", lambda name, settings: False)
    return home


def specs(body: str) -> dict[str, Any]:
    return {key: json.loads(text) for key, text in SPEC.findall(body)}


def expected_spec(db: Path, dig_id: str, params: DigParams) -> Any:
    with Store.open(db, read_only=True) as store:
        dig = registry.discover()[dig_id]
        return web_app.card(dig, dig.compute(store, params)).spec


def attrs(body: str, element_id: str) -> dict[str, str]:
    tag = re.search(rf'<[a-z]+[^>]*\bid="{element_id}"[^>]*>', body, re.DOTALL)
    assert tag, element_id
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', tag.group()))


# ---- Every page renders ---------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_the_demo_db(client: ClientFactory, demo_db: Path, path: str) -> None:
    response = client(demo_db, demo=True).get(path)

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CSP
    assert 'aria-current="page"' in response.text
    assert t("onboarding.demo.banner") in response.text


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("/messages", "empty.messages.title"),
        ("/music", "empty.music.title"),
        ("/money", "empty.money.title"),
        ("/timeline", "empty.timeline.title"),
        ("/ask", "empty.ask.title"),
        ("/settings", "empty.settings.title"),
    ],
)
@pytest.mark.parametrize("database", ["empty", "missing"])
def test_every_page_renders_without_data(
    client: ClientFactory, empty_db: Path, tmp_path: Path, path: str, key: str, database: str
) -> None:
    db = empty_db if database == "empty" else tmp_path / "missing.duckdb"
    response = client(db).get(path)

    assert response.status_code == 200
    assert t(key).replace("'", "&#39;") in response.text
    assert 'class="chart" data-chart=' not in response.text


# ---- Domain pages -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "prefix"), [("/messages", "messages."), ("/music", "music."), ("/money", "money.")]
)
def test_domain_page_has_one_card_per_dig_and_its_headlines(
    client: ClientFactory, demo_db: Path, path: str, prefix: str
) -> None:
    body = client(demo_db).get(path).text
    with Store.open(demo_db, read_only=True) as store:
        digs = [d for d in registry.available(store) if d.id.startswith(prefix)]

    assert digs
    cards = re.findall(
        r'<figure class="card chart-card[^"]*" aria-labelledby="dig-([a-z-]+)-title"', body
    )
    assert sorted(cards) == sorted(d.id.replace(".", "-").replace("_", "-") for d in digs)
    assert body.count('<dl class="tile">') >= 1
    assert '<form class="filters" method="get"' in body
    assert 'id="filter-status" role="status"' in body


def test_messages_reading_order_puts_the_overview_first(
    client: ClientFactory, demo_db: Path
) -> None:
    body = client(demo_db).get("/messages").text
    order = re.findall(r'id="dig-(messages-[a-z-]+)-title"', body)

    assert order[:3] == [
        "messages-volume-by-contact",
        "messages-top-contacts-by-year",
        "messages-activity-heatmap",
    ]


def test_filters_change_the_dig_params(client: ClientFactory, demo_db: Path) -> None:
    query = "date_from=2025-01-01&date_to=2025-12-31&top_n=5&granularity=year"
    body = client(demo_db).get(f"/messages?{query}").text
    params = DigParams(
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
        top_n=5,
        granularity="year",
        tz=local_zone(),
    )

    assert attrs(body, "dig-results") == {
        "date-from": "2025-01-01",
        "date-to": "2025-12-31",
        "top-n": "5",
        "granularity": "year",
        "currency": "",
        "tz": local_zone(),
    }
    rendered = specs(body)
    for dig_id in ("messages.volume_by_contact", "messages.response_times"):
        key = "dig-" + dig_id.replace(".", "-").replace("_", "-") + "-spec"
        assert rendered[key] == expected_spec(demo_db, dig_id, params)
        assert rendered[key] != expected_spec(demo_db, dig_id, DigParams(tz=local_zone()))
    assert "Showing 1 Jan 2025 to 31 Dec 2025, grouped by year, top 5." in body
    # The form keeps the values, so a reload or a second change starts from them.
    assert 'name="date_from" value="2025-01-01"' in body
    assert '<option value="5" selected>' in body
    assert '<option value="year" selected>' in body


def test_money_currency_filter(client: ClientFactory, demo_db: Path) -> None:
    default = client(demo_db).get("/money").text
    eur = client(demo_db).get("/money?currency=eur").text

    with Store.open(demo_db, read_only=True) as store:
        currencies = _pages.currencies(store)
    assert currencies[0] == "RON"
    assert "EUR" in currencies
    assert attrs(default, "dig-results")["currency"] == "RON"
    assert attrs(eur, "dig-results")["currency"] == "EUR"
    assert '<option value="EUR" selected>' in eur
    assert "Amounts in EUR." in eur
    params = DigParams(tz=local_zone(), currency="EUR")
    assert specs(eur)["dig-money-spend-by-category-spec"] == expected_spec(
        demo_db, "money.spend_by_category", params
    )
    assert specs(eur) != specs(default)


def test_unknown_filter_values_fall_back_and_say_so(client: ClientFactory, demo_db: Path) -> None:
    body = (
        client(demo_db)
        .get("/money?date_from=yesterday&top_n=7&granularity=decade&currency=XYZ")
        .text
    )

    assert attrs(body, "dig-results") | {"tz": ""} == {
        "date-from": "",
        "date-to": "",
        "top-n": "10",
        "granularity": "month",
        "currency": "RON",
        "tz": "",
    }
    assert 'class="notice notice-warning"' in body
    assert "From is not a date" in body
    assert "no transactions in that currency" in body


def test_reversed_range_shows_the_whole_range(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/music?date_from=2025-06-01&date_to=2025-01-01").text

    assert "From is after To" in body
    assert attrs(body, "dig-results")["date-from"] == ""


def test_range_without_rows_shows_the_no_results_state(
    client: ClientFactory, demo_db: Path
) -> None:
    body = client(demo_db).get("/music?date_from=2001-01-01&date_to=2001-12-31").text

    assert t("empty.no_results.title") in body
    assert 'class="chart" data-chart=' not in body


def test_htmx_filter_request_swaps_only_the_results(client: ClientFactory, demo_db: Path) -> None:
    headers = HX | {"HX-Target": "dig-results"}
    response = client(demo_db).get("/messages?top_n=5", headers=headers)
    body = response.text

    assert response.status_code == 200
    assert body.lstrip().startswith('<div class="dig-results" id="dig-results"')
    assert "<html" not in body
    assert '<form class="filters"' not in body
    assert 'id="filter-status" role="status" hx-swap-oob="true"' in body
    assert attrs(body, "dig-results")["top-n"] == "5"
    assert 'class="chart" data-chart=' in body


def test_htmx_history_restore_gets_the_whole_page(client: ClientFactory, demo_db: Path) -> None:
    headers = HX | {"HX-Target": "dig-results", "HX-History-Restore-Request": "true"}
    body = client(demo_db).get("/messages", headers=headers).text

    assert "<html" in body


def test_filter_form_works_without_javascript(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/money").text
    form = body[body.index('<form class="filters"') : body.index("</form>")]

    assert 'method="get" action="/money"' in form
    assert 'type="submit"' in form
    # HTMX may enhance it, but must never keep personal data in its history cache.
    assert 'hx-history="false"' in form


def test_page_lists_digs_that_wait_for_another_table(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NeedsLocations:
        id = "messages.near_home"
        title = "Messages near home"
        requires: ClassVar[list[str]] = ["messages", "locations"]

        def compute(self, store: Store, params: DigParams) -> Any:
            raise AssertionError("not available, never computed")

    found = registry.discover()
    monkeypatch.setattr(
        registry, "discover", lambda: {**found, NeedsLocations.id: NeedsLocations()}
    )
    body = client(demo_db).get("/messages").text

    assert "This chart needs messages, locations." in body
    assert "location history" in body


# ---- Timeline ---------------------------------------------------------------------------------


def test_timeline_sections_in_order(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/timeline").text
    order = [
        body.index('id="dig-cross-life-timeline-title"'),
        body.index('id="dig-cross-soundtrack-title"'),
        body.index('id="dig-trends-yoy-title"'),
        body.index('id="dig-cross-spend-vs-listening-title"'),
        body.index('id="dig-cross-messages-vs-youtube-title"'),
    ]

    assert order == sorted(order)
    assert 'class="chart-card chart-card-wide"' in body or "chart-card-wide" in body
    assert body.count(t("common.correlation_note")) >= 2
    assert t("pages.timeline.changes_title") in body
    assert '<fieldset class="segmented">' in body


def test_timeline_controls_change_the_dig_params(client: ClientFactory, demo_db: Path) -> None:
    body = (
        client(demo_db)
        .get("/timeline?granularity=week&date_from=2025-03-01&date_to=2025-05-31")
        .text
    )
    tz = local_zone()

    assert attrs(body, "timeline-controls") == {
        "granularity": "week",
        "date-from": "2025-03-01",
        "date-to": "2025-05-31",
        "tz": tz,
    }
    assert 'id="granularity-week" checked' in body
    rendered = specs(body)
    assert rendered["dig-cross-life-timeline-spec"] == expected_spec(
        demo_db, "cross.life_timeline", DigParams(granularity="week", tz=tz)
    )
    soundtrack = DigParams(date_from=date(2025, 3, 1), date_to=date(2025, 5, 31), tz=tz)
    assert rendered["dig-cross-soundtrack-spec"] == expected_spec(
        demo_db, "cross.soundtrack", soundtrack
    )


def test_timeline_htmx_request_swaps_only_the_controls(
    client: ClientFactory, demo_db: Path
) -> None:
    headers = HX | {"HX-Target": "timeline-controls"}
    body = client(demo_db).get("/timeline?granularity=year", headers=headers).text

    assert body.lstrip().startswith('<form class="timeline-controls" id="timeline-controls"')
    assert "dig-trends-yoy" not in body
    assert attrs(body, "timeline-controls")["granularity"] == "year"


def test_timeline_trends_table_shows_headline_changes(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/timeline").text
    table = body[body.index('id="changes-title"') :]
    table = table[: table.index("</table>")]
    with Store.open(demo_db, read_only=True) as store:
        rows = registry.discover()["trends.yoy"].compute(store, DigParams(tz=local_zone())).data

    assert rows.num_rows > 0
    assert table.count('<th scope="row">') == rows.num_rows
    assert str(rows.column("label")[0]) in table


def test_timeline_shows_an_empty_state_for_missing_digs(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = {
        k: v for k, v in registry.discover().items() if not k.startswith(("cross.", "trends."))
    }
    monkeypatch.setattr(registry, "discover", lambda: found)
    monkeypatch.setattr(registry, "available", lambda store: list(found.values()))
    body = client(demo_db).get("/timeline").text

    assert body.count(t("empty.dig_missing.title").replace("'", "&#39;")) == 5
    assert "cross.life_timeline" in body
    assert "trends.yoy" in body


def test_timeline_shows_an_empty_state_for_a_failing_dig(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soundtrack = registry.discover()["cross.soundtrack"]

    def broken(store: Store, params: DigParams) -> Any:
        raise ValueError("synthetic failure")

    monkeypatch.setattr(soundtrack, "compute", broken)
    body = client(demo_db).get("/timeline").text

    assert t("empty.dig_failed.title", title=soundtrack.title) in body
    assert "dig-cross-life-timeline-title" in body


# ---- Settings ---------------------------------------------------------------------------------


def test_settings_shows_the_import_ledger(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db, demo=True).get("/settings").text
    with Store.open(demo_db, read_only=True) as store:
        ledger = store.query(
            "SELECT connector, tz, rows_inserted, finished_at FROM imports"
            " WHERE status = 'succeeded' ORDER BY finished_at DESC LIMIT 1"
        ).to_pylist()[0]

    row = body[body.index(">Synthetic demo data <") :]
    row = row[: row.index("</tr>")]
    assert f"{ledger['rows_inserted']:,}" in row
    assert ledger["tz"] in row
    finished = ledger["finished_at"].astimezone(UTC)
    assert f"{finished.day} {finished.strftime('%b')} {finished.year}" in row
    for name in ("WhatsApp", "Spotify", "Bank CSV"):
        assert f'<th scope="row">{name} <code' in body
    assert t("pages.settings.not_used") in body
    assert t("pages.settings.demo_title") in body


def test_settings_key_status_never_shows_key_values(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "sk-sentinel-0123456789-do-not-show"
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)
    monkeypatch.setattr(ask, "PROBE", lambda name, settings: name == "anthropic")
    web = client(demo_db)
    bodies = [web.get(path).text for path in PAGES]
    body = bodies[PAGES.index("/settings")]

    assert all(sentinel not in page and sentinel[:12] not in page for page in bodies)
    anthropic = body[body.index('<th scope="row">Anthropic</th>') :]
    anthropic = anthropic[: anthropic.index("</tr>")]
    assert "ANTHROPIC_API_KEY" in anthropic
    assert "key-set" in anthropic
    openai = body[body.index('<th scope="row">OpenAI</th>') :]
    assert "key-unset" in openai[: openai.index("</tr>")]
    assert "claude-sonnet-5-5" in body  # the resolved model


def write_privacy(home: Path, data: dict[str, Any]) -> None:
    (home / "privacy.json").write_text(json.dumps({"version": 1, **data}), encoding="utf-8")


COUNTS = {"requests": 2, "bytes_sent": 14465, "bytes_received": 102}


def test_privacy_counter_zero(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/settings").text

    assert '<div class="card privacy-card">' in body
    assert '<span class="num privacy-counter-num">0</span>' in body
    assert t("pages.settings.sent_none") in body
    assert 'id="remote-title"' not in body
    assert 'id="local-title"' not in body
    assert t("privacy.counter_zero") in body  # the nav badge


def test_privacy_counter_local_only(client: ClientFactory, demo_db: Path, sherd_home: Path) -> None:
    write_privacy(sherd_home, {"remote": {}, "local": {"ollama": COUNTS}})
    body = client(demo_db).get("/settings").text

    assert '<span class="num privacy-counter-num">0</span>' in body
    assert "privacy-card-sent" not in body
    local = body[body.index(t("pages.settings.local_title")) :]
    assert '<th scope="row">Ollama</th>' in local
    assert "14,465" in local
    assert 'id="remote-title"' not in body
    assert t("privacy.counter_zero") in body


def test_privacy_counter_remote(client: ClientFactory, demo_db: Path, sherd_home: Path) -> None:
    write_privacy(sherd_home, {"remote": {"anthropic": COUNTS}, "local": {"ollama": COUNTS}})
    web = client(demo_db)
    body = web.get("/settings").text

    assert "privacy-card privacy-card-sent" in body
    assert '<span class="num privacy-counter-num">14,465</span>' in body
    assert t("pages.settings.sent_remote", requests="2", providers=1) in body
    remote = body[body.index(t("pages.settings.remote_title")) :]
    assert '<th scope="row">Anthropic</th>' in remote
    assert t("pages.settings.local_title") in body
    # The nav badge on every page carries the same number.
    for path in ["/", *PAGES]:
        page = web.get(path).text
        badge = page[page.index('class="privacy-badge"') :]
        badge = badge[: badge.index("</a>")]
        assert "14.1 KB sent to 1 provider" in badge
        assert t("privacy.counter_zero") not in badge


def test_withdrawing_consent_edits_config(
    client: ClientFactory, demo_db: Path, sherd_home: Path
) -> None:
    config.record_consent("anthropic")
    config.record_consent("openai")
    web = client(demo_db)
    page = web.get("/settings").text
    assert page.count('action="/settings/consent"') == 2

    response = web.post(
        "/settings/consent",
        data={"csrf": _pages.CSRF_TOKEN, "provider": "anthropic"},
        headers={"Origin": "http://127.0.0.1:8765"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings#llm-title"
    assert not config.has_consent("anthropic")
    assert config.has_consent("openai")


@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ({"provider": "anthropic"}, {"Origin": "http://127.0.0.1:8765"}),
        ({"provider": "anthropic", "csrf": "forged"}, {"Origin": "http://127.0.0.1:8765"}),
        ({"provider": "anthropic", "csrf": "TOKEN"}, {"Origin": "http://evil.example"}),
        ({"provider": "anthropic", "csrf": "TOKEN"}, {}),
    ],
)
def test_withdrawing_consent_needs_csrf_and_same_origin(
    client: ClientFactory, demo_db: Path, data: dict[str, str], headers: dict[str, str]
) -> None:
    config.record_consent("anthropic")
    if data.get("csrf") == "TOKEN":
        data = {**data, "csrf": _pages.CSRF_TOKEN}
    response = client(demo_db).post("/settings/consent", data=data, headers=headers)

    assert response.status_code == 403
    assert response.headers["content-security-policy"] == CSP
    assert config.has_consent("anthropic")


def test_settings_reports_a_broken_config(
    client: ClientFactory, demo_db: Path, sherd_home: Path
) -> None:
    (sherd_home / "config.json").write_text("[1, 2]", encoding="utf-8")
    (sherd_home / "privacy.json").write_text("not json at all", encoding="utf-8")
    response = client(demo_db, raise_server_exceptions=False).get("/settings")

    assert response.status_code == 200
    assert "config.json" in response.text
    assert 'class="notice notice-warning"' in response.text


# ---- Shared rules: CSP, remote URLs, inline code, escaping -------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_pages_reference_no_remote_urls_or_inline_code(
    client: ClientFactory, demo_db: Path, empty_db: Path, path: str
) -> None:
    for body in (client(demo_db, demo=True).get(path).text, client(empty_db).get(path).text):
        assert {u for u in URL.findall(body) if u not in NAMESPACES} == set()
        scripts = re.findall(r"<script\b([^>]*)>", body)
        assert all("src=" in a or 'type="application/json"' in a for a in scripts)
        assert " style=" not in body
        assert "<style" not in body
        assert not re.search(r"\son[a-z]+=", body)


PAYLOAD = "<script>alert(1)</script>\"'><img src=x onerror=alert(2)>"


@pytest.fixture
def hostile_db(tmp_path: Path) -> Path:
    """Every name and text field a page might show carries a script/quote payload."""
    path = tmp_path / "hostile.duckdb"
    start = datetime(2024, 1, 1, 9, tzinfo=UTC)
    with Store.open(path) as store:
        import_id = store.begin_import("whatsapp", "1", "hostile", "UTC")
        store.upsert(
            import_id,
            "whatsapp",
            [
                Message(
                    source_file="chat.txt",
                    source_row_id=f"m{i}",
                    chat_id=f"chat-{i % 3}",
                    chat_name=f"{PAYLOAD} {i % 3}",
                    chat_kind="direct",
                    sender_id=f"whatsapp:name:{PAYLOAD}",
                    sender_name=f"{PAYLOAD} {i % 3}",
                    is_from_me=i % 2 == 0,
                    ts=start + timedelta(hours=7 * i),
                    text=f"{PAYLOAD} 😀 word{i % 5}",
                    kind="text",
                )
                for i in range(400)
            ],
        )
        store.upsert(
            import_id,
            "spotify",
            [
                MediaPlay(
                    source_file="plays.json",
                    source_row_id=f"p{i}",
                    ts=start + timedelta(hours=5 * i),
                    media_kind="track",
                    artist=f"{PAYLOAD} {i % 4}",
                    track=f"{PAYLOAD} track {i % 6}",
                    ms_played=180_000,
                    skipped=i % 7 == 0,
                )
                for i in range(600)
            ],
        )
        store.upsert(
            import_id,
            "bank_csv",
            [
                Transaction(
                    source_file="statement.csv",
                    source_row_id=f"t{i}",
                    ts=start + timedelta(days=i),
                    amount=Decimal("-12.50") if i % 9 else Decimal("2000"),
                    currency="EUR",
                    merchant_raw=PAYLOAD,
                    merchant=f"{PAYLOAD} {i % 3}",
                    category=f"{PAYLOAD} cat {i % 2}",
                    account=PAYLOAD,
                    kind="card",
                )
                for i in range(500)
            ],
        )
        store.upsert(
            import_id,
            "google_takeout",
            [
                Event(
                    source_file="watch.json",
                    source_row_id=f"e{i}",
                    ts=start + timedelta(hours=11 * i),
                    kind="youtube.watch",
                    title=PAYLOAD,
                )
                for i in range(300)
            ],
        )
        store.finish_import(import_id, "succeeded")
    return path


def test_hostile_data_renders_escaped_on_every_page(
    client: ClientFactory, hostile_db: Path, sherd_home: Path
) -> None:
    (sherd_home / "config.json").write_text(json.dumps({"ask": {"provider": "stub"}}))
    web = client(hostile_db)
    bodies = {path: web.get(path).text for path in ["/", *PAGES]}
    bodies["/messages?top_n=50&granularity=week"] = web.get(
        "/messages?top_n=50&granularity=week"
    ).text
    bodies["/timeline?granularity=day"] = web.get("/timeline?granularity=day").text

    assert PAYLOAD.replace("<", "&lt;").replace(">", "&gt;")[:30] in bodies["/messages"]
    for path, body in bodies.items():
        assert "<script>alert" not in body, path
        assert "<img src=x" not in body, path
        for key, text in SPEC.findall(body):
            assert "</script" not in text.lower(), (path, key)
            json.loads(text)
