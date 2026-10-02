"""Incremental Takeout activity markup reader; one record per outer cell."""

import re
from collections import deque
from html.parser import HTMLParser
from typing import Any

from sherd_connectors.google_takeout.dates import normalise_spaces


class ActivityHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: deque[dict[str, Any]] = deque()
        self.depth = 0
        self.outer_depth = 0
        self.cell_depth = 0
        self.cell = ""
        self.header = ""
        self.lines: list[str] = []
        self.links: list[dict[str, str]] = []
        self.cells: list[tuple[list[str], list[dict[str, str]]]] = []
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
                self.outer_depth = self.depth
                self.header, self.cells = "", []
                self.cell, self.href = "", None
            if self.active and ("content-cell" in classes or "header-cell" in classes):
                self.cell = "content" if "content-cell" in classes else "header"
                self.cell_depth = self.depth
                self.lines, self.links = [""], []
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
            self.links.append({"name": normalise_spaces(self.link_text), "url": self.href})
            self.href = None
        if tag == "div":
            if self.active and self.depth == self.cell_depth:
                if self.cell == "content":
                    self.cells.append(
                        (
                            [normalise_spaces(line) for line in self.lines if line.strip()],
                            self.links,
                        )
                    )
                self.cell = ""
            if self.active and self.depth == self.outer_depth:
                self.finish_entry()
                self.active = False
                self.cells = []
            self.depth -= 1

    def finish_entry(self) -> None:
        # Body cells contain a clock; headers/empty side cells/captions do not.
        body = next(
            (
                (lines, links)
                for lines, links in self.cells
                if any(re.search(r"\d{1,2}:\d{2}:\d{2}", line) for line in lines)
            ),
            self.cells[0] if self.cells else ([], []),
        )
        lines, links = body
        self.records.append(
            {
                "header": normalise_spaces(self.header),
                "title": lines[0] if lines else "",
                "titleUrl": links[0]["url"] if links else None,
                "subtitles": links[1:],
                "time": next(
                    (line for line in reversed(lines) if re.search(r"\d{1,2}:\d{2}:\d{2}", line)),
                    "",
                ),
                "details": [{"name": line} for cell_lines, _ in self.cells for line in cell_lines],
            }
        )
