"""CLI entry point and module-based command discovery."""

import importlib
import logging
import os
import pkgutil
import re
from copy import copy
from typing import Annotated

import typer

from sherd_cli import commands


def malformed_count(record: logging.LogRecord) -> int:
    """Read connector count-only diagnostics."""
    if not record.name.startswith(("sherd.connectors.", "sherd_connectors.")):
        return 0
    message = record.getMessage()
    if "skipped" not in message:
        return 0
    match = re.search(r"(?:count|malformed)=(\d+)", message)
    return int(match[1]) if match else 0


class _ConsoleLogs(logging.Filter):
    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        if malformed_count(record):
            record = copy(record)
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return record if record.levelno >= self.level else False


def _configure_logging(ctx: typer.Context, verbose: bool) -> None:
    level = (
        logging.DEBUG
        if os.environ.get("SHERD_DEBUG") == "1"
        else (logging.INFO if verbose else logging.WARNING)
    )
    root = logging.getLogger()
    previous_level = root.level
    handler = logging.StreamHandler()
    handler.addFilter(_ConsoleLogs(level))
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.setLevel(level)
    root.addHandler(handler)

    def restore() -> None:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)

    ctx.call_on_close(restore)


def create_app() -> typer.Typer:
    """Build an application from the commands exposed by installed modules."""
    app = typer.Typer(invoke_without_command=True)

    # Keep group mode when only one command has been discovered.
    @app.callback()
    def root(
        ctx: typer.Context,
        verbose: Annotated[bool, typer.Option("--verbose", help="Show informational logs")] = False,
    ) -> None:
        """Explore the data exports you already own.

        Example: sherd demo
        """
        _configure_logging(ctx, verbose)
        if ctx.invoked_subcommand is None:
            typer.echo(
                "sherd — explore the data exports you already own.\n"
                "dig PATH  Import an export.\n"
                "ask QUESTION  Ask about your data.\n"
                "show [ID]  Explore insights.\n"
                "web  Open local dashboards.\n"
                "demo  Build synthetic data to try it.\n"
                "Run sherd COMMAND --help for options and examples."
            )

    for module_info in pkgutil.iter_modules(commands.__path__, commands.__name__ + "."):
        module = importlib.import_module(module_info.name)
        register = getattr(module, "register", None)
        if callable(register):
            register(app)
    return app


app = create_app()


def main() -> None:
    """Run the discovered commands."""
    app()
