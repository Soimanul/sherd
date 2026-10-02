"""Keyboard-driven, read-only terminal dashboards."""

from collections.abc import Iterable
from typing import ClassVar

from rich.text import Text
from sherd_core import Store
from sherd_insights import DigParams, DigResult, registry
from sherd_insights.base import Dig
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

DOMAINS = ("Messages", "Music", "Money", "Timeline")


def domain(dig: Dig) -> str:
    prefix = dig.id.split(".", 1)[0]
    return {"messages": "Messages", "music": "Music", "money": "Money"}.get(prefix, "Timeline")


class Dashboard(App[None]):
    """Inspect headline tiles, then open a dig without modifying the database."""

    TITLE = "sherd · your data, locally"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("question_mark", "help", "Help"),
        ("right", "headlines", "Digs"),
        ("left", "domains", "Domains"),
    ]
    # Mapped from sherd_web/static/tokens.css: bg, surfaces, text, accent and focus.
    CSS = """
    Screen { background: #171412; color: #f3ebe3; }
    Header, Footer { background: #1f1c19; color: #ccc2ba; }
    #body { height: 1fr; }
    #domains { width: 18; background: #171412; border-right: solid #403b37; }
    #content { width: 1fr; padding: 1 2; }
    #domain-title { color: #ec9668; text-style: bold; height: 2; }
    #headlines { height: 12; background: #1f1c19; }
    OptionList > .option-list--option-highlighted { background: #41261b; color: #ec9668; }
    OptionList:focus { border: tall #f5c076; }
    #narrative { height: auto; max-height: 8; margin: 1 0; }
    #data { height: 1fr; background: #1f1c19; }
    DataTable > .datatable--header { background: #282421; color: #ccc2ba; }
    DataTable > .datatable--cursor { background: #41261b; color: #ec9668; }
    Footer > .footer--key { background: #1f1c19; color: #ec9668; }
    * { scrollbar-color: #817a75; scrollbar-background: #282421;
        scrollbar-color-hover: #cd6b44; scrollbar-color-active: #cd6b44; }
    #help { height: auto; color: #ccc2ba; display: none; }
    """

    def __init__(self, store: Store, params: DigParams | None = None) -> None:
        super().__init__()
        if not store.read_only:
            raise ValueError("Dashboard needs a read-only store.")
        self.store = store
        self.params = params or DigParams()
        self.selected_domain = DOMAINS[0]
        self.selected_dig: str | None = None
        self.digs = registry.available(store)
        self.results: dict[str, DigResult] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield OptionList(*DOMAINS, id="domains")
            with Vertical(id="content"):
                yield Static("Messages", id="domain-title")
                yield OptionList(id="headlines")
                yield Static("", id="narrative", markup=False)
                yield DataTable(id="data")
                yield Static(
                    "↑/↓ select · Enter open · → digs · ← domains · Tab table · q quit · ? help",
                    id="help",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.load_domain(DOMAINS[0])
        self.query_one("#domains", OptionList).focus()

    def load_domain(self, name: str) -> None:
        self.selected_domain = name
        self.selected_dig = None
        self.query_one("#domain-title", Static).update(name)
        choices = self.query_one("#headlines", OptionList)
        choices.clear_options()
        self.query_one("#data", DataTable).clear(columns=True)
        matching = [dig for dig in self.digs if domain(dig) == name]
        for dig in matching:
            if dig.id not in self.results:
                self.results[dig.id] = dig.compute(self.store, self.params)
            result = self.results[dig.id]
            headline = result.headline
            metric = (
                f"{headline.value} {headline.unit or ''} · {headline.label}"
                if headline
                else result.narrative
            )
            choices.add_option(Option(Text(f"{dig.title}\n{metric}"), id=dig.id))
        choices.highlighted = 0 if matching else None
        self.query_one("#narrative", Static).update(
            "Select a headline and press Enter to explore."
            if matching
            else "No data here yet. Run `sherd demo` or `sherd dig PATH`."
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "domains":
            self.load_domain(DOMAINS[event.option_index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "domains":
            self.action_headlines()
        elif event.option.id:
            self.selected_dig = event.option.id
            result = self.results[event.option.id]
            self.query_one("#narrative", Static).update(result.narrative)
            table = self.query_one("#data", DataTable)
            table.clear(columns=True)
            table.add_columns(*result.data.column_names)
            rows: Iterable[tuple[Text, ...]] = (
                tuple(Text(str(row[name])) for name in result.data.column_names)
                for row in result.data.to_pylist()
            )
            table.add_rows(rows)

    def action_headlines(self) -> None:
        self.query_one("#headlines", OptionList).focus()

    def action_domains(self) -> None:
        self.query_one("#domains", OptionList).focus()

    def action_help(self) -> None:
        help_text = self.query_one("#help", Static)
        help_text.display = not help_text.display
