"""Display the installed CLI version."""

import typer

from sherd_cli import __version__


def register(app: typer.Typer) -> None:
    """Register the version command."""

    @app.command()
    def version() -> None:
        typer.echo(f"sherd {__version__}")
