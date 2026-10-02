"""Ask: the `sherd ask` pipeline behind a CSRF-protected form, consent, and offline mode."""

import json
import socket
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sherd_agent import config, privacy
from sherd_agent.providers import ChatMessage, Completion, Provider
from sherd_agent.providers.base import body_size
from sherd_agent.providers.stub import StubProvider
from sherd_core import Store
from sherd_web.deps import t
from sherd_web.routes import _pages, ask

ClientFactory = Callable[..., TestClient]

ORIGIN = {"Origin": "http://127.0.0.1:8765"}
HX = {"HX-Request": "true", "HX-Target": "ask-result"}
SQL = "SELECT kind, count(*) AS n FROM messages GROUP BY kind ORDER BY n DESC"
QUESTION = "What kinds of messages do I send?"


class FakeRemote:
    """A remote provider that answers from a script and never opens a socket."""

    name = "anthropic"
    model = "claude-sonnet-5-5"
    remote = True

    def __init__(self, plan: Mapping[str, Any]) -> None:
        self.plan = plan
        self.calls: list[list[ChatMessage]] = []

    def complete(
        self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
    ) -> Completion:
        self.calls.append(list(messages))
        text = json.dumps(self.plan) if json_schema is not None else "Mostly text."
        return Completion(text, 900, 40, body_size(messages), len(text))


@pytest.fixture(autouse=True)
def sherd_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SHERD_HOME", str(home))
    monkeypatch.setattr(ask, "PROBE", lambda name, settings: False)
    return home


Use = Callable[..., None]


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch, sherd_home: Path) -> Use:
    """Make a provider the one Ask resolves; `configured` also pins it in config.json."""

    def choose(provider: Provider, *, configured: str | None = None) -> None:
        monkeypatch.setattr(ask, "PROBE", lambda name, settings: name == provider.name)
        monkeypatch.setattr(ask, "CREATE", lambda name, model, settings: provider)
        if configured:
            config_file = sherd_home / "config.json"
            config_file.write_text(json.dumps({"ask": {"provider": configured}}))

    return choose


def stub(plans: Mapping[str, Any]) -> StubProvider:
    return StubProvider({k: dict(v) for k, v in plans.items()}, answer="Stub answer: text wins.")


def post(web: TestClient, data: dict[str, str], headers: Mapping[str, str] = ORIGIN) -> Any:
    return web.post("/ask", data={"csrf": _pages.CSRF_TOKEN, **data}, headers=dict(headers))


def counts(db: Path) -> dict[str, int]:
    with Store.open(db, read_only=True) as store:
        return store.table_counts()


# ---- The page ---------------------------------------------------------------------------------


def test_ask_page_has_a_csrf_protected_post_form(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    use(stub({}), configured="stub")
    body = client(demo_db).get("/ask").text

    assert '<form class="ask-form card" method="post" action="/ask"' in body
    assert f'name="csrf" value="{_pages.CSRF_TOKEN}"' in body
    assert 'name="offline" value="on"' in body
    assert t("privacy.local_model", provider="the stub provider") in body
    for example in ("Who did I message most in 2024?", "What did I listen to on Sunday mornings?"):
        assert f'name="example" value="{example}"' in body


def test_ask_has_no_get_api(client: ClientFactory, demo_db: Path) -> None:
    web = client(demo_db)

    assert web.get("/ask?question=hi").status_code == 200  # the page, nothing asked
    assert web.put("/ask").status_code == 405
    assert web.get("/ask.json").status_code == 404


def test_unreadable_config_is_reported_not_a_crash(
    client: ClientFactory, demo_db: Path, sherd_home: Path
) -> None:
    (sherd_home / "config.json").write_text("[1, 2]", encoding="utf-8")
    web = client(demo_db, raise_server_exceptions=False)
    page = web.get("/ask")
    answer = post(web, {"question": QUESTION})

    for response in (page, answer):
        assert response.status_code == 200
        assert t("pages.ask.config_broken_title").replace("'", "&#39;") in response.text
        assert "config.json" in response.text


def test_no_provider_explains_ollama_and_api_keys(client: ClientFactory, demo_db: Path) -> None:
    body = client(demo_db).get("/ask").text

    assert t("empty.ask_no_model.title") in body
    assert t("pages.ask.ollama_title") in body
    assert "ollama pull qwen2.5-coder:7b" in body
    assert t("pages.ask.key_title") in body
    assert '<form class="ask-form' not in body


# ---- Answers ----------------------------------------------------------------------------------


def test_sql_answer_shows_the_sql_first(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = stub({QUESTION: {"sql": SQL}})
    use(provider, configured="stub")
    response = post(client(demo_db), {"question": QUESTION})
    body = response.text

    assert response.status_code == 200
    sql = body.index('<pre id="ask-query-text">')
    rows = body.index('<p class="ask-meta">')
    answer = body.index("Stub answer: text wins.")
    chart = body.index('data-chart="ask-chart-spec"')
    table = body.index('id="ask-table-title"')
    assert sql < rows < answer < chart < table
    assert '<span class="k">SELECT</span> kind, <span class="f">count</span>' in body
    assert "4 rows" in body or "3 rows" in body
    assert t("common.copy_sql") in body
    assert '<th scope="col">kind</th>' in body
    assert len(provider.calls) == 2


def test_dig_answer_shows_the_dig_and_params_first(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    plan = {"dig": "music.top_artists_by_year", "params": {"top_n": 3}}
    use(stub({QUESTION: plan}), configured="stub")
    body = post(client(demo_db), {"question": QUESTION}).text

    query = body.index(
        '<pre id="ask-query-text"><span class="k">dig</span> music.top_artists_by_year'
    )
    assert query < body.index("Stub answer: text wins.")
    assert "&#34;top_n&#34;: 3" in body
    assert t("pages.ask.dig_label") in body


def test_example_button_asks_its_question(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    example = "Who did I message most in 2024?"
    use(stub({example: {"sql": SQL}}), configured="stub")
    body = post(client(demo_db), {"question": "", "example": example}).text

    assert f"</span>{example}</h2>" in body
    assert '<pre id="ask-query-text">' in body


def test_refusal(client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use) -> None:
    use(stub({QUESTION: {"refuse": "nothing in the tables says that"}}), configured="stub")
    body = post(client(demo_db), {"question": QUESTION}).text

    assert t("pages.ask.refused_title") in body
    assert "nothing in the tables says that" in body
    assert '<pre id="ask-query-text">' not in body


def test_guard_rejects_writes_and_shows_the_sql(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    before = counts(demo_db)
    use(stub({QUESTION: {"sql": "DELETE FROM messages"}}), configured="stub")
    body = post(client(demo_db), {"question": QUESTION}).text

    assert 'class="notice notice-danger"' in body
    assert "only SELECT/WITH reads are allowed" in body
    assert 'id="ask-query-text">DELETE <span class="k">FROM</span> messages</pre>' in body
    assert t("pages.ask.sql_label_failed") in body
    assert counts(demo_db) == before


def test_empty_and_overlong_questions(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = stub({})
    use(provider, configured="stub")
    web = client(demo_db)

    assert t("pages.ask.no_question") in post(web, {"question": "   "}).text
    assert "Questions can be up to 2,000 characters." in post(web, {"question": "x" * 2001}).text
    assert provider.calls == []


def test_htmx_post_returns_only_the_result(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    use(stub({QUESTION: {"sql": SQL}}), configured="stub")
    body = post(client(demo_db), {"question": QUESTION}, ORIGIN | HX).text

    assert body.lstrip().startswith('<div class="ask-result" id="ask-result">')
    assert "<html" not in body
    assert "data-focus" in body


def test_ask_on_an_empty_database(
    client: ClientFactory, empty_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = stub({QUESTION: {"sql": SQL}})
    use(provider, configured="stub")
    body = post(client(empty_db), {"question": QUESTION}).text

    assert t("empty.ask.title") in body
    assert provider.calls == []


# ---- The POST guard ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("csrf", "headers"),
    [
        (None, ORIGIN),
        ("", ORIGIN),
        ("forged-token", ORIGIN),
        ("TOKEN", {"Origin": "http://evil.example"}),
        ("TOKEN", {"Origin": "http://localhost:8765"}),  # another origin, even on loopback
        ("TOKEN", {"Origin": "null", "Referer": "http://evil.example/page"}),
        ("TOKEN", {"Sec-Fetch-Site": "cross-site"}),
        ("TOKEN", {}),  # no evidence of where the form came from
    ],
)
def test_post_without_token_or_from_another_origin_is_403(
    client: ClientFactory,
    demo_db: Path,
    sherd_home: Path,
    csrf: str | None,
    headers: dict[str, str],
    use: Use,
) -> None:
    provider = stub({QUESTION: {"sql": SQL}})
    use(provider, configured="stub")
    data = {"question": QUESTION}
    if csrf is not None:
        data["csrf"] = _pages.CSRF_TOKEN if csrf == "TOKEN" else csrf
    response = client(demo_db).post("/ask", data=data, headers=headers)

    assert response.status_code == 403
    assert response.headers["content-security-policy"]
    assert provider.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        ORIGIN,
        {"Origin": "null", "Sec-Fetch-Site": "same-origin"},  # what browsers send under no-referrer
        {"Referer": "http://127.0.0.1:8765/ask"},
    ],
)
def test_same_origin_post_is_accepted(
    client: ClientFactory, demo_db: Path, sherd_home: Path, headers: dict[str, str], use: Use
) -> None:
    use(stub({QUESTION: {"sql": SQL}}), configured="stub")
    response = post(client(demo_db), {"question": QUESTION}, headers)

    assert response.status_code == 200
    assert '<pre id="ask-query-text">' in response.text


def test_oversized_form_is_refused(client: ClientFactory, demo_db: Path) -> None:
    response = client(demo_db).post(
        "/ask", data={"csrf": _pages.CSRF_TOKEN, "question": "x" * 70_000}, headers=ORIGIN
    )

    assert response.status_code == 413


# ---- Remote providers: consent and offline ----------------------------------------------------


def test_remote_provider_needs_consent_first(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider)
    body = post(client(demo_db), {"question": QUESTION}).text

    assert provider.calls == []
    assert privacy.remote_bytes_sent() == 0
    assert not config.has_consent("anthropic")
    card = body[body.index('<section class="card consent"') :]
    assert t("pages.ask.consent.title", provider="Anthropic") in card
    assert f"<q>{QUESTION}</q>" in card
    assert t("pages.ask.consent.catalog").replace("'", "&#39;") in card
    assert "at most 50 result rows, as no more than 8 KB of CSV" in card
    assert "cut to 200 characters" in card
    assert "### messages" in card  # the exact planning prompt, catalog included
    assert 'name="consent" value="allow"' in card
    assert 'name="consent" value="cancel"' in card
    assert 'name="provider" value="anthropic"' in card
    assert f'name="csrf" value="{_pages.CSRF_TOKEN}"' in card


def test_allowing_records_consent_and_answers(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider)
    web = client(demo_db)
    body = post(web, {"question": QUESTION, "consent": "allow", "provider": "anthropic"}).text

    assert config.has_consent("anthropic")
    stored = json.loads((sherd_home / "config.json").read_text())
    assert set(stored["ask"]["consent"]) == {"anthropic"}
    assert len(provider.calls) == 2
    assert '<pre id="ask-query-text">' in body
    assert "Mostly text." in body
    sent = privacy.remote_bytes_sent()
    assert sent > 0
    assert f"{sent:,} bytes sent in 2 requests" in body
    # Next time the question goes straight through, and the nav badge counts it.
    again = post(web, {"question": QUESTION}).text
    assert '<section class="card consent"' not in again
    assert len(provider.calls) == 4
    badge = again[again.index('class="privacy-badge"') :]
    assert "sent to 1 provider" in badge[: badge.index("</a>")]


def test_cancel_sends_nothing(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider)
    body = post(
        client(demo_db), {"question": QUESTION, "consent": "cancel", "provider": "anthropic"}
    ).text

    assert t("pages.ask.cancelled") in body
    assert provider.calls == []
    assert not config.has_consent("anthropic")


def test_consent_for_another_provider_is_not_recorded(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider)
    body = post(
        client(demo_db), {"question": QUESTION, "consent": "allow", "provider": "openai"}
    ).text

    assert '<section class="card consent"' in body
    assert provider.calls == []
    assert not config.has_consent("anthropic")


def test_offline_refuses_a_configured_remote_provider(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider, configured="anthropic")
    config.record_consent("anthropic")
    body = post(client(demo_db), {"question": QUESTION, "offline": "on"}).text

    assert t("pages.ask.offline_refused_title") in body
    assert "--offline refuses the remote provider" in body
    assert provider.calls == []
    assert privacy.remote_bytes_sent() == 0


def test_offline_skips_remote_providers_when_none_is_configured(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    provider = FakeRemote({"sql": SQL})
    use(provider)
    body = post(client(demo_db), {"question": QUESTION, "offline": "on"}).text

    assert t("pages.ask.offline_no_local") in body
    assert provider.calls == []


def test_offline_blocks_the_network_while_asking(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    class Probe:
        name, model, remote = "stub", "canned", False
        blocked: ClassVar[list[str]] = []

        def complete(
            self, messages: Sequence[ChatMessage], *, json_schema: Mapping[str, Any] | None
        ) -> Completion:
            try:
                socket.create_connection(("192.0.2.1", 443), timeout=0.1)
            except OSError as error:
                self.blocked.append(type(error).__name__)
            text = json.dumps({"sql": SQL}) if json_schema else "ok"
            return Completion(text, 0, 0, body_size(messages), len(text))

    probe = Probe()
    use(probe, configured="stub")
    post(client(demo_db), {"question": QUESTION, "offline": "on"})

    assert probe.blocked == ["OfflineError", "OfflineError"]


def test_hostile_question_is_escaped(
    client: ClientFactory, demo_db: Path, sherd_home: Path, use: Use
) -> None:
    hostile = "<script>alert(1)</script>\"'><img src=x onerror=alert(2)>"
    provider = FakeRemote({"sql": SQL})
    use(provider)
    body = post(client(demo_db), {"question": hostile}).text

    assert "<script>alert" not in body
    assert "<img src=x" not in body
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt;&#34;&#39;&gt;&lt;img' in body
    sql = "SELECT '<b>x</b>' AS \"</pre><script>\""
    use(stub({QUESTION: {"sql": sql}}), configured="stub")
    answer = post(client(demo_db), {"question": QUESTION}).text
    assert "<b>x</b>" not in answer
    assert "</pre><script>" not in answer


def test_sql_tokens_cover_the_text() -> None:
    sql = "WITH a AS (SELECT 'it''s' AS s, 1.5 AS n -- note\n FROM t) SELECT upper(s) FROM a"
    tokens = ask.sql_tokens(sql)

    assert "".join(text for _, text in tokens) == sql
    assert ("k", "WITH") in tokens
    assert ("s", "'it''s'") in tokens
    assert ("n", "1.5") in tokens
    assert ("c", "-- note") in tokens
    assert ("f", "upper") in tokens


@pytest.mark.parametrize("state", ["zero", "local", "remote", "unknown", "permission"])
def test_badge_states_agree_with_settings(
    state: str,
    client: ClientFactory,
    demo_db: Path,
    sherd_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if state in ("local", "remote"):
        privacy.privacy_path().write_text(
            json.dumps(
                {
                    "remote": {"anthropic": {"bytes_sent": 2048}} if state == "remote" else {},
                    "local": {"ollama": {"requests": 1}},
                }
            )
        )
    elif state == "unknown":
        privacy.privacy_path().write_text('{"remote": []}')
    elif state == "permission":
        privacy.privacy_path().write_text("{}")

        def denied() -> dict[str, Any]:
            raise PermissionError("synthetic")

        monkeypatch.setattr(privacy, "load", denied)
    body = client(demo_db).get("/settings").text
    badge = body[body.index('class="privacy-badge"') :].split("</a>")[0]
    expected = "unknown" if state == "permission" else state
    assert f'data-state="{expected}"' in badge
    if expected == "remote":
        assert "#i-up" in badge
        assert "#i-shield" not in badge
        assert "2.0 KB sent to 1 provider" in badge
    elif expected == "unknown":
        assert body.count("Privacy ledger unreadable") >= 2
        assert "0 bytes sent" not in badge
    else:
        assert "#i-shield" in badge
        assert "0 bytes sent" in badge
        if expected == "local":
            assert body.count("local model") >= 2


def test_remote_ask_returns_fresh_oob_badge(
    client: ClientFactory,
    demo_db: Path,
    sherd_home: Path,
    use: Use,
) -> None:
    use(FakeRemote({"sql": SQL}))
    body = post(
        client(demo_db),
        {"question": QUESTION, "consent": "allow", "provider": "anthropic"},
        ORIGIN | HX,
    ).text
    from sherd_web.deps import privacy_state

    assert 'id="privacy-badge"' in body
    assert 'hx-swap-oob="outerHTML"' in body
    assert privacy_state()["human"] + " sent to 1 provider" in body


def test_provider_error_detail_is_bounded_and_logs_only_class(
    client: ClientFactory,
    demo_db: Path,
    use: Use,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sherd_agent.providers import ProviderError

    use(stub({}), configured="stub")

    def fail(*args: Any) -> Provider:
        raise ProviderError("ERROR_SENTINEL_\x00" + "x" * 150000)

    monkeypatch.setattr(ask, "CREATE", fail)
    body = post(client(demo_db), {"question": QUESTION}, ORIGIN | HX).text
    assert "Provider unavailable." in body
    assert "x" * 301 not in body
    assert "\x00" not in body
    assert len(body) < 5000
    assert "ProviderError" in caplog.text
    assert "ERROR_SENTINEL" not in caplog.text


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ValueError("only SELECT/WITH reads are allowed"), "safety guard"),
        (TimeoutError("timed out"), "timed out"),
        (ValueError("model did not return a valid plan"), "model output was invalid"),
        (ValueError("query failed " + "x" * 150000), "could not be answered"),
    ],
)
def test_error_categories_and_query_detail_budget(error: Exception, message: str) -> None:
    displayed = ask.display_error(error)
    assert message in displayed
    assert len(displayed.split(". ", 1)[1]) <= 300


def test_failed_remote_ask_returns_accounted_oob_badge(
    client: ClientFactory,
    demo_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sherd_agent.providers import ProviderError

    def failed_attempt(*args: Any, **kwargs: Any) -> ask.View:
        privacy.privacy_path().write_text(
            json.dumps({"remote": {"anthropic": {"bytes_sent": 2048}}, "local": {}})
        )
        raise ProviderError("synthetic transport failure")

    monkeypatch.setattr(ask, "_run", failed_attempt)
    body = post(client(demo_db), {"question": QUESTION}, ORIGIN | HX).text
    assert 'hx-swap-oob="outerHTML"' in body
    assert "2.0 KB sent to 1 provider" in body
    assert "Provider unavailable." in body


def test_wrapped_guard_errors_keep_the_error_category() -> None:
    from sherd_agent.ask import AskError
    from sherd_agent.sql_guard import SqlRejectedError

    def fail() -> None:
        try:
            raise SqlRejectedError("exactly one statement is allowed")
        except SqlRejectedError:
            raise AskError("query failed") from None

    with pytest.raises(AskError) as caught:
        fail()
    assert "safety guard" in ask.display_error(caught.value)
