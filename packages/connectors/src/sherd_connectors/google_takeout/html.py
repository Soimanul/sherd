"""Incremental Takeout activity markup reader; only one outer cell is retained."""

from collections import deque
from html.parser import HTMLParser
from typing import Any


class ActivityHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: deque[dict[str, Any]] = deque()
        self.depth = 0
        self.cell_depth = 0
        self.cell = ""
        self.header = ""
        self.lines: list[str] = []
        self.links: list[dict[str, str]] = []
        self.href: str | None = None
        self.link_text = ""
        self.active = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if "outer-cell" in classes:
                self.active = True
                self.header, self.lines, self.links = "", [""], []
            if self.active and ("content-cell" in classes or "header-cell" in classes):
                self.cell = "content" if "content-cell" in classes else "header"
                self.cell_depth = self.depth
        if self.active and tag == "p" and not self.cell:
            self.cell = "header"
        if self.active and self.cell == "content":
            if tag == "br":
                self.lines.append("")
            if tag == "a":
                self.href = attributes.get("href")
                self.link_text = ""

    def handle_data(self, data: str) -> None:
        if self.active and self.cell == "header":
            self.header += data
        if self.active and self.cell == "content":
            self.lines[-1] += data
            if self.href is not None:
                self.link_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.cell == "header":
            self.cell = ""
        if tag == "a" and self.href is not None:
            self.links.append({"name": self.link_text.strip(), "url": self.href})
            self.href = None
        if tag == "div":
            if self.depth == self.cell_depth:
                if self.cell == "content":
                    lines = [line.strip() for line in self.lines if line.strip()]
                    self.records.append(
                        {
                            "header": self.header.strip(),
                            "title": lines[0] if lines else "",
                            "titleUrl": self.links[0]["url"] if self.links else None,
                            "subtitles": self.links[1:],
                            "time": next(
                                (
                                    line
                                    for line in reversed(lines)
                                    if ":" in line and any(c.isdigit() for c in line)
                                ),
                                "",
                            ),
                            "details": [{"name": line} for line in lines[1:]],
                        }
                    )
                self.cell = ""
            self.depth -= 1
