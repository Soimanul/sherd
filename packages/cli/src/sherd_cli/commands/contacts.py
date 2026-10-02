"""Inspect contacts and confirm or reject identity matches."""

from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sherd_core.entities import decide, resolve
from sherd_core.paths import default_db_path
from sherd_core.store import Store, StoreError

from sherd_cli.commands.demo import demo_db_path


def register(app: typer.Typer) -> None:
    contacts = typer.Typer(invoke_without_command=True, help="List and resolve contacts.")
    app.add_typer(contacts, name="contacts")

    @contacts.callback()
    def options(
        ctx: typer.Context,
        db: Annotated[Path | None, typer.Option(help="Database path")] = None,
        demo: Annotated[bool, typer.Option(help="Use the demo database")] = False,
    ) -> None:
        if db is not None and demo:
            Console(stderr=True).print("error: choose --db or --demo", markup=False)
            raise typer.Exit(2)
        ctx.obj = db or (demo_db_path() if demo else default_db_path())
        if ctx.invoked_subcommand is None:
            execute(ctx, "list")

    def execute(ctx: typer.Context, action: str, a: str = "", b: str = "") -> None:
        console = Console()
        try:
            with Store.open(ctx.obj) as store:
                if action == "list":
                    table = Table("id", "name", "messages", "transfers")
                    for row in store.query("""SELECT c.id, c.display_name,
                        (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS messages,
                        (SELECT count(*) FROM transactions t WHERE t.contact_id = c.id) AS transfers
                        FROM contacts c WHERE merged_into IS NULL ORDER BY display_name, id
                    """).to_pylist():
                        table.add_row(
                            row["id"],
                            row["display_name"],
                            str(row["messages"]),
                            str(row["transfers"]),
                        )
                    console.print(table)
                else:
                    report = (
                        decide(store, a, b, "merge" if action == "merge" else "reject")
                        if action in ("merge", "reject")
                        else resolve(store)
                    )
                    if action == "proposals":
                        table = Table("identity a", "identity b", "score")
                        for proposal in report.proposals:
                            table.add_row(proposal.a, proposal.b, f"{proposal.score:.2f}")
                        console.print(table)
                    console.print(
                        f"{report.auto_merged} automatic merges; "
                        f"{len(report.proposals)} proposals; "
                        f"{report.assigned_messages} message and "
                        f"{report.assigned_transactions} transfer assignments changed."
                    )
        except (OSError, ValueError, StoreError, duckdb.Error) as error:
            Console(stderr=True).print(f"[red]error:[/] {escape(str(error))}")
            raise typer.Exit(1) from None

    @contacts.command(name="resolve")
    def resolve_command(ctx: typer.Context) -> None:
        """Resolve identities and assign imported facts."""
        execute(ctx, "resolve")

    @contacts.command()
    def proposals(ctx: typer.Context) -> None:
        """Show possible matches requiring confirmation."""
        execute(ctx, "proposals")

    @contacts.command()
    def merge(ctx: typer.Context, a: str, b: str) -> None:
        """Confirm a match between two contact ids or identity keys."""
        execute(ctx, "merge", a, b)

    @contacts.command()
    def reject(ctx: typer.Context, a: str, b: str) -> None:
        """Persistently reject a match between two contact ids or identity keys."""
        execute(ctx, "reject", a, b)
