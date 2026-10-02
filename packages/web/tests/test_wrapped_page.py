import re
from pathlib import Path
from typing import Any

import pytest
from sherd_core import Store
from sherd_web import app as web_app
from sherd_web.routes import wrapped as page

from .conftest import ClientFactory


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    page._decks.clear()


def card_sources(html: str) -> list[str]:
    return re.findall(r'<img src="(/wrapped/[^"]+)"', html)


def test_page_lists_six_cards_with_downloads(demo_db: Path, client: ClientFactory) -> None:
    got = client(demo_db, demo=True).get("/wrapped")
    assert got.status_code == 200
    assert got.headers["content-security-policy"] == web_app.CSP
    assert got.headers["cache-control"] == "no-store"
    html = got.text
    sources = card_sources(html)
    assert sources == [
        f"/wrapped/2025/{n}.png?year=2025&amp;names=0&amp;amounts=0" for n in range(1, 7)
    ]
    for n in range(1, 7):
        assert f'download="wrapped-2025-{n}.png"' in html
    assert '<option value="2025" selected>2025 (last full year)</option>' in html
    assert 'aria-current="page"' in html  # the shell marks Wrapped as the current page
    assert "Lavinia Owlcroft" not in html  # initials only by default, alt text included
    assert 'alt="Your circle in 2025: LO, 788 messages' in html
    assert "<style" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)", html)


def test_pngs_come_from_memory_with_the_same_headers(demo_db: Path, client: ClientFactory) -> None:
    http = client(demo_db)
    for n in range(1, 7):
        got = http.get(f"/wrapped/2025/{n}.png")
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-security-policy"] == web_app.CSP
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-disposition"] == f'inline; filename="wrapped-2025-{n}.png"'
        assert got.content[:8] == b"\x89PNG\r\n\x1a\n"
    again = http.get("/wrapped/2025/1.png")
    assert again.content == http.get("/wrapped/2025/1.png").content
    assert len(page._decks) == 1


def test_png_routes_reject_cards_that_do_not_exist(
    demo_db: Path, empty_db: Path, tmp_path: Path, client: ClientFactory
) -> None:
    http = client(demo_db)
    assert http.get("/wrapped/2025/7.png").status_code == 404
    assert http.get("/wrapped/2025/0.png").status_code == 404
    assert http.get("/wrapped/1999/1.png").status_code == 404
    assert http.get("/wrapped/0/1.png").status_code == 404
    assert http.get("/wrapped/99999/1.png").status_code == 404
    assert client(tmp_path / "missing.duckdb").get("/wrapped/2025/1.png").status_code == 404
    assert client(empty_db).get("/wrapped/2025/1.png").status_code == 404


def test_toggles_show_names_and_amounts(demo_db: Path, client: ClientFactory) -> None:
    http = client(demo_db)
    # The form sends a hidden 0 before each checkbox; a ticked box's 1 comes last and wins.
    html = http.get("/wrapped?year=2025&names=0&names=1&amounts=0&amounts=1").text
    assert "Lavinia Owlcroft" in html
    assert card_sources(html)[0].endswith("names=1&amp;amounts=1")
    assert re.search(r'name="names" value="1" checked', html)
    names_png = http.get("/wrapped/2025/2.png?names=1&amounts=1").content
    assert names_png != http.get("/wrapped/2025/2.png").content


def test_other_years_and_bad_years(demo_db: Path, client: ClientFactory) -> None:
    http = client(demo_db)
    html = http.get("/wrapped?year=2024").text
    assert card_sources(html)[0].startswith("/wrapped/2024/1.png")
    assert len(card_sources(html)) == 5  # 2023 began in October: no change card for 2024
    for bad in ("1999", "twenty", "-1"):
        html = http.get(f"/wrapped?year={bad}").text
        assert "nothing to wrap for that year" in html
        assert card_sources(html)[0].startswith("/wrapped/2025/1.png")


def test_empty_states(empty_db: Path, tmp_path: Path, client: ClientFactory) -> None:
    for db in (empty_db, tmp_path / "missing.duckdb"):
        got = client(db).get("/wrapped")
        assert got.status_code == 200
        assert "Not enough to wrap yet." in got.text
        assert "sherd dig" in got.text
        assert not card_sources(got.text)


def test_the_page_only_opens_read_only_stores(
    demo_db: Path, client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = Store.open

    def spy(path: Path, **kwargs: Any) -> Store:
        calls.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(Store, "open", spy)
    http = client(demo_db)
    assert http.get("/wrapped").status_code == 200
    assert http.get("/wrapped/2025/1.png").status_code == 200
    assert calls
    assert all(call.get("read_only") is True for call in calls)
