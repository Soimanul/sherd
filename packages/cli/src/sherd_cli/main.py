"""CLI entry point and module-based command discovery."""

import importlib
import pkgutil

import typer

from sherd_cli import commands


def create_app() -> typer.Typer:
    """Build an application from the commands exposed by installed modules."""
    app = typer.Typer()

    # Keep group mode when only one command has been discovered.
    @app.callback()
    def root() -> None:
        """Explore the data exports you already own."""

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
