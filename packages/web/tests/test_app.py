import re
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sherd_core import Store
from sherd_core.store import ReadOnlyStoreError
from sherd_insights import DigParams, DigResult, Headline, registry
from sherd_web import app as web_app
from sherd_web.deps import STATIC_DIR, TEMPLATES_DIR, cell, open_store, t

ClientFactory = Callable[..., TestClient]

# Spelled out rather than imported, so the test pins the exact policy.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
URL = re.compile(r"https?://[^\s\"'<>)`]+")
NAMESPACES = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}
# Alpine's CSP build names its plugin docs in an error message; nothing requests it.
ALLOWED_IN_FILE = {"vendor/alpine-csp.min.js": {"https://alpinejs.dev/plugins/${r}"}}
# Static files WP-14b owns; the rest of static/ belongs to the design system (WP-14a).
OWNED_STATIC = [
    "app.css",
    "app.js",
    "vendor/htmx.min.js",
    "vendor/alpine-csp.min.js",
    "vendor/vega-interpreter.min.js",
]


def remote_urls(text: str, allowed: AbstractSet[str] = frozenset()) -> set[str]:
    return {url for url in URL.findall(text) if url not in NAMESPACES | allowed}


def available_with_headline(db: Path) -> int:
    with Store.open(db, read_only=True) as store:
        digs = registry.available(store)
        return sum(1 for dig in digs if dig.compute(store, DigParams()).headline is not None)


# ---- Home -------------------------------------------------------------------------------------


def test_home_on_demo_db_shows_tiles_and_three_charts(client: ClientFactory, demo_db: Path) -> None:
    response = client(demo_db, demo=True).get("/")

    assert response.status_code == 200
    body = response.text
    assert body.count('<dl class="tile">') == available_with_headline(demo_db) > 0
    assert body.count('class="chart" data-chart=') == 3
    assert body.count('<script type="application/json" id="dig-') == 3
    assert "Top contacts by year" in body  # the preferred first chart
    assert body.count(t("common.view_table")) == 3
    assert t("onboarding.demo.banner") in body
    assert t("common.demo_label") in body
    assert t("empty.home.title") not in body


def test_chart_card_parts(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/").text
    card = body[body.index('<figure class="card chart-card') : body.index("</figure>")]

    assert 'role="img"' in card
    assert 'aria-describedby="dig-messages-top-contacts-by-year-summary"' in card
    assert '<figcaption class="chart-summary" id="dig-messages-top-contacts-by-year-summary">' in (
        card
    )
    assert "Yearly contact ranks" in card  # the dig's text_summary, visible to everyone
    assert '<details class="chart-table">' in card
    assert '<th scope="col"' in card
    assert "$schema" not in card
    assert '"config"' in card  # the theme travels inside the spec


def test_home_nav_marks_current_page(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/").text
    nav = body[body.index('<nav class="nav"') : body.index("</nav>")]

    assert re.search(r'href="/" aria-current="page"', nav)
    assert nav.count('aria-current="page"') == 1
    for href in ("/messages", "/music", "/money", "/timeline", "/ask", "/wrapped", "/settings"):
        assert f'href="{href}"' in nav
    assert '<span class="nav-count"' in nav  # row counts for the demo tables
    assert 'class="skip-link" href="#main"' in body
    assert 'id="main"' in body
    assert t("privacy.counter_zero") in body


@pytest.mark.parametrize("kind", ["empty", "missing"])
def test_home_without_data_shows_onboarding(
    client: ClientFactory, empty_db: Path, tmp_path: Path, kind: str
) -> None:
    db = empty_db if kind == "empty" else tmp_path / "nowhere" / "life.duckdb"

    response = client(db).get("/")

    assert response.status_code == 200
    body = response.text
    assert t("onboarding.title") in body
    assert t("empty.home.title") in body
    assert t("empty.home.action") in body
    assert t("empty.home.secondary") in body
    assert "sherd demo &amp;&amp; sherd web --demo" in body
    assert "data-chart" not in body
    if kind == "missing":
        assert not db.exists()  # the app never creates a database


def test_failed_dig_is_reported_and_skipped(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass(frozen=True)
    class Broken:
        id: str = "test.broken"
        title: str = "Broken test dig"
        requires: list[str] = field(default_factory=lambda: ["messages"])

        def compute(self, store: Store, params: DigParams) -> DigResult:
            raise ValueError("boom")

    real = registry.available
    monkeypatch.setattr(registry, "available", lambda store: [Broken(), *real(store)])

    response = client(demo_db).get("/")

    assert response.status_code == 200
    assert "1 dig could not be computed: Broken test dig." in response.text
    assert response.text.count('class="chart" data-chart=') == 3


def test_chart_data_cannot_break_out_of_its_script_block(
    client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "</script><script>alert(1)</script>"

    @dataclass(frozen=True)
    class Hostile:
        id: str = "test.hostile"
        title: str = "Hostile test dig"
        requires: list[str] = field(default_factory=lambda: ["messages"])

        def compute(self, store: Store, params: DigParams) -> DigResult:
            data = pa.table({"name": [hostile], "n": [1]})
            chart = {"data": {"values": [{"name": hostile, "n": 1}]}, "mark": "bar"}
            return DigResult(data, chart, hostile, Headline(hostile, 1), hostile)

    monkeypatch.setattr(web_app, "PREFERRED", ("test.hostile",))
    monkeypatch.setattr(registry, "available", lambda store: [Hostile()])

    body = client(demo_db).get("/").text

    assert hostile not in body
    assert "\\u003c/script\\u003e" in body  # escaped inside the JSON block
    assert "&lt;/script&gt;" in body  # escaped in the HTML


# ---- Errors -----------------------------------------------------------------------------------


def test_404_page(client: ClientFactory, demo_db: Path) -> None:
    response = client(demo_db).get("/no-such-page")

    assert response.status_code == 404
    assert "Nothing dug up here." in response.text
    assert "<code>/no-such-page</code>" in response.text
    assert 'href="/"' in response.text


def test_500_page(client: ClientFactory, demo_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(store: Store) -> list[object]:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(registry, "available", explode)

    response = client(demo_db, raise_server_exceptions=False).get("/")

    assert response.status_code == 500
    assert "Something went wrong." in response.text
    assert "secret detail" not in response.text
    assert response.headers["content-security-policy"] == CSP


@pytest.mark.parametrize(
    "path", ["/static/design/preview.html", "/static/Design/preview.html", "/static/./design/"]
)
def test_design_preview_is_not_served(client: ClientFactory, demo_db: Path, path: str) -> None:
    assert client(demo_db).get(path).status_code == 404


def test_foreign_host_is_refused(client: ClientFactory, demo_db: Path) -> None:
    response = client(demo_db).get("/", headers={"host": "attacker.example"})

    assert response.status_code == 400
    assert response.headers["content-security-policy"] == CSP


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8765", "127.0.0.1:8765", "[::1]", "[::1]:8765"]
)
def test_loopback_host_forms_are_allowed(client: ClientFactory, tmp_path: Path, host: str) -> None:
    assert client(tmp_path / "missing.duckdb").get("/", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.test",
        "evil.test:8765",
        "localhost.evil.test",
        "localhost.evil.test:8765",
        "127.0.0.1.evil.test",
        "[::2]",
        "[::2]:8765",
        "localhost:bad",
        "localhost:",
        "localhost:12:34",
        "[::1]:bad",
        "[::1]:",
        "[::1]:12:34",
        "[::1",
        "::1",
        "user@localhost",
        "localhost@evil.test",
        "user@[::1]:8765",
    ],
)
def test_foreign_and_malformed_host_forms_are_refused(
    client: ClientFactory, tmp_path: Path, host: str
) -> None:
    response = client(tmp_path / "missing.duckdb").get("/", headers={"host": host})
    assert response.status_code == 400
    assert response.headers["content-security-policy"] == CSP


# ---- CSP and remote references ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/no-such-page",
        "/static/app.css",
        "/static/app.js",
        "/static/tokens.css",
        "/static/owl.svg",
        "/static/vendor/vega.min.js",
        "/static/vendor/htmx.min.js",
        "/static/fonts/FiraSans-Regular.woff2",
        "/static/design/preview.html",
    ],
)
def test_every_response_carries_the_csp(client: ClientFactory, demo_db: Path, path: str) -> None:
    response = client(demo_db).get(path)

    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_owned_templates_and_static_files_reference_no_remote_urls() -> None:
    files = {
        str(path.relative_to(TEMPLATES_DIR)): path.read_text(encoding="utf-8")
        for path in TEMPLATES_DIR.rglob("*.html")
    } | {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in OWNED_STATIC}

    assert {"base.html", "shell/home.html", "shell/error.html"} <= files.keys()
    found = {
        name: urls
        for name, text in files.items()
        if (urls := remote_urls(text, ALLOWED_IN_FILE.get(name, set())))
    }
    assert found == {}


def test_rendered_pages_reference_no_remote_urls(
    client: ClientFactory, demo_db: Path, empty_db: Path
) -> None:
    bodies = [
        client(demo_db, demo=True).get("/").text,
        client(empty_db).get("/").text,
        client(demo_db).get("/no-such-page").text,
    ]

    assert [remote_urls(body) for body in bodies] == [set(), set(), set()]


def test_no_inline_scripts_or_styles(client: ClientFactory, demo_db: Path, empty_db: Path) -> None:
    for body in (client(demo_db, demo=True).get("/").text, client(empty_db).get("/").text):
        scripts = re.findall(r"<script\b([^>]*)>", body)
        assert scripts
        assert all("src=" in attrs or 'type="application/json"' in attrs for attrs in scripts)
        assert " style=" not in body
        assert "<style" not in body
        assert not re.search(r"\son[a-z]+=", body)  # no inline event handlers


# ---- Read-only store --------------------------------------------------------------------------


def test_app_store_is_read_only(demo_db: Path) -> None:
    app = web_app.create_app(demo_db, demo=True)
    dependency = open_store(app.state.settings)
    store = next(dependency)
    try:
        assert store is not None
        assert store.read_only
        before = store.table_counts()
        with pytest.raises(ReadOnlyStoreError):
            store.begin_import("synthetic", "1", "x", "UTC")
        # Below the Store API, the DuckDB connection itself refuses writes.
        with pytest.raises(duckdb.Error, match="read-only"):
            store._conn.execute("DELETE FROM messages")
        assert store.table_counts() == before
    finally:
        dependency.close()


# ---- Helpers ----------------------------------------------------------------------------------


def test_copy_lookup_fills_placeholders() -> None:
    assert t("empty.home.hint", db_path="~/x.duckdb").endswith("~/x.duckdb.")
    assert t("common.rows") == "{count} rows"  # unknown placeholders stay visible
    with pytest.raises(KeyError):
        t("empty.nope.title")


def test_summary_lists_tables_with_rows() -> None:
    counts = {"messages": 22348, "media_plays": 14665, "transactions": 0, "events": 3}
    assert web_app.summary(counts) == "22,348 messages, 14,665 plays and 3 events"
    assert web_app.summary({"messages": 1}) == "1 message"


def test_table_cells_keep_years_whole(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/").text

    assert '<td class="r">2023</td>' in body or '<td class="r">2024</td>' in body
    assert not re.search(r'<td class="r">2,0\d\d</td>', body)
    assert cell(1048, "messages") == "1,048"
    assert cell(2024, "year") == "2024"


@pytest.mark.parametrize(
    ("path", "policy"),
    [("/", "no-store"), ("/no-such-page", "no-store"), ("/static/app.css", "no-cache")],
)
def test_cache_policy(client: ClientFactory, demo_db: Path, path: str, policy: str) -> None:
    assert client(demo_db).get(path).headers["cache-control"] == policy
