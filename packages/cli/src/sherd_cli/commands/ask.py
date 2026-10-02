"""`sherd ask "…"`: answer a question with an LLM-written plan, always showing the SQL (PLAN §7)."""

import json
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from sherd_agent import config, providers
from sherd_agent.ask import Asker, AskError, AskResult
from sherd_agent.llm import LLM, NoProviderError, OfflineRefusedError, resolve
from sherd_agent.providers import ProviderError
from sherd_agent.providers.netguard import OfflineError, offline_guard
from sherd_core import Store
from sherd_core.paths import default_db_path, sherd_home
from sherd_core.store import StoreError
from sherd_insights.charts import json_value, records

from sherd_cli.commands.dig import interactive, local_zone

PREVIEW_ROWS = 20


class ConsentRefusedError(Exception):
    """A remote provider was not allowed to receive the question."""


def disclosure(provider: str, model: str, system_prompt: str, question: str) -> str:
    return (
        f"`sherd ask` is about to send data to {provider} ({model}), a remote service.\n"
        "This is exactly what the first request sends (the planning prompt with your database's\n"
        "catalog, row counts and date ranges, and the list of insights; no rows):\n"
        f"{'-' * 72}\n{system_prompt}\n{'-' * 72}\n"
        f"Your question: {question}\n{'-' * 72}\n"
        "A second request sends the question, the query, the row count and at most 50 result rows\n"
        "AND at most 8 KB (8192 bytes) of CSV in total, including the header. Each cell is\n"
        "limited to 200 characters, truncated with … when needed.\n"
        "API keys are read from the environment and\n"
        "never stored. Consent is remembered for this provider in $SHERD_HOME/config.json."
    )


def _print_result(console: Console, result: AskResult) -> None:
    if result.kind == "refuse":
        console.print(f"Not answerable: {result.refusal}", markup=False, highlight=False)
        return
    if result.kind == "dig":
        params = json.dumps(result.params, ensure_ascii=False, sort_keys=True)
        console.print(f"Dig: {result.dig} {params}", markup=False, highlight=False)
    else:
        console.print(f"SQL: {result.sql}", markup=False, highlight=False)
    rows = f"Rows: {result.row_count}"
    if result.truncated:
        rows += " (truncated at the 10 000-row budget)"
    console.print(rows, markup=False, highlight=False)
    console.print(result.answer, markup=False, highlight=False)
    if result.table is not None and result.row_count:
        table = Table()
        for name in result.columns:
            table.add_column(name)
        for row in records(result.table.slice(0, PREVIEW_ROWS)):
            table.add_row(*(Text("" if row[n] is None else str(row[n])) for n in result.columns))
        console.print(table)
        if result.row_count > PREVIEW_ROWS:
            console.print(f"… {result.row_count - PREVIEW_ROWS} more rows (use --json)")
    if result.chart is not None:
        console.print("A chart is available: --json includes its Vega-Lite spec.")


def _print_usage(console: Console, result: AskResult) -> None:
    if not result.remote:
        return
    usage = result.usage
    console.print(
        f"Tokens: {usage.input_tokens} in, {usage.output_tokens} out over {usage.requests}"
        f" requests to {result.provider} ({result.model}); {usage.bytes_sent} bytes sent.",
        markup=False,
        highlight=False,
    )


def register(app: typer.Typer) -> None:
    @app.command()
    def ask(
        question: Annotated[str, typer.Argument(help="Your question, in plain words")],
        provider: Annotated[
            str | None, typer.Option(help="ollama, anthropic, openai or stub")
        ] = None,
        model: Annotated[str | None, typer.Option(help="Model id for the provider")] = None,
        offline: Annotated[
            bool, typer.Option(help="Refuse remote providers and block non-loopback network")
        ] = False,
        db: Annotated[Path | None, typer.Option()] = None,
        demo: Annotated[bool, typer.Option(help="Ask the demo database")] = False,
        json_output: Annotated[bool, typer.Option("--json")] = False,
        yes: Annotated[bool, typer.Option("--yes", help="Consent to a remote provider")] = False,
    ) -> None:
        """Ask a question about your data; the SQL (or insight) used is always shown."""
        console = Console(soft_wrap=True)
        guard: AbstractContextManager[None] = offline_guard() if offline else nullcontext()
        try:
            with guard:
                path = db or (sherd_home() / "demo.duckdb" if demo else default_db_path())
                if not path.is_file():
                    typer.echo("No database yet. Run `sherd demo` or `sherd dig PATH`.", err=True)
                    raise typer.Exit(1)
                settings = providers.load_settings()
                choice = resolve(
                    settings,
                    provider=provider,
                    model=model,
                    config=config.load(),
                    offline=offline,
                    probe=providers.available,
                )
                llm = LLM(providers.create(choice.provider, choice.model, settings))
                with Store.open(path, read_only=True, sandboxed=True) as store:
                    asker = Asker(store, llm, tz=local_zone())
                    if choice.remote and not config.has_consent(choice.provider):
                        text = disclosure(
                            choice.provider, choice.model, asker.system_prompt(), question
                        )
                        Console(stderr=True, soft_wrap=True).print(
                            text, markup=False, highlight=False
                        )
                        if not yes and not (
                            interactive() and typer.confirm("Send it?", default=False)
                        ):
                            raise ConsentRefusedError(
                                f"nothing was sent; pass --yes to allow {choice.provider}"
                            )
                        config.record_consent(choice.provider)
                    try:
                        result = asker.ask(question)
                    except AskError as error:
                        if error.sql and not json_output:
                            console.print(f"SQL: {error.sql}", markup=False, highlight=False)
                        raise
            if json_output:
                typer.echo(json.dumps(json_value(result.to_json()), ensure_ascii=False))
                return
            _print_result(console, result)
            _print_usage(console, result)
        except NoProviderError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from None
        except (
            AskError,
            ConsentRefusedError,
            OfflineRefusedError,
            OfflineError,
            ProviderError,
            OSError,
            ValueError,
            StoreError,
            duckdb.Error,
        ) as error:
            Console(stderr=True).print(f"error: {error}", markup=False, highlight=False)
            raise typer.Exit(1) from None
