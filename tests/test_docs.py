"""Docs are complete and do not load third-party resources."""

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

from sherd_connectors.registry import discover as connectors
from sherd_insights.registry import discover as digs

ROOT = Path(__file__).resolve().parents[1]


class RemoteResources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "img", "iframe", "source"}:
            self.urls.append(values.get("src") or "")
        if tag == "link" and values.get("rel") != "canonical":
            self.urls.append(values.get("href") or "")


def test_docs_build_and_coverage(tmp_path: Path) -> None:
    site = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    landing = (site / "index.html").read_text()
    assert (
        "curl -LsSf https://raw.githubusercontent.com/Soimanul/sherd/main/scripts/install.sh | sh"
        in landing
    )
    connector_index = (site / "connectors/index.html").read_text()
    for connector_id in connectors():
        assert connector_id in connector_index
        assert (site / f"connectors/{connector_id}/index.html").exists()
    digs_index = (site / "digs/index.html").read_text()
    for dig_id in digs():
        assert dig_id in digs_index
    for html in site.rglob("*.html"):
        parser = RemoteResources()
        parser.feed(html.read_text())
        assert not [url for url in parser.urls if url.startswith(("http:", "https:", "//"))], html
    for css in site.rglob("*.css"):
        text = css.read_text()
        assert "url(https:" not in text, css
        assert "url(http:" not in text, css


def test_distribution_licenses_match_root() -> None:
    root_license = (ROOT / "LICENSE").read_bytes()
    for package in [*sorted((ROOT / "packages").glob("*")), ROOT / "crates/sherd-wa"]:
        assert (package / "LICENSE").read_bytes() == root_license
