"""`sherd dig PATH`: detect an export's connector and import it (PLAN §5)."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import typer
from rich.console import Console
from rich.markup import escape
from sherd_connectors import registry
from sherd_connectors.base import Connector, DetectResult, ImportContext, export_root
from sherd_connectors.pipeline import run_import
from sherd_core import Store
from sherd_core.entities import resolve
from sherd_core.paths import default_db_path
from sherd_core.store import StoreError

from sherd_cli import config

AMBIGUOUS_GAP = 0.1
MIN_CONFIDENCE = 0.5
DEBUG_ENV = "SHERD_DEBUG"

Ranked = list[tuple[Connector, DetectResult]]


class DigError(Exception):
    """An expected failure, reported as one line with an exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def local_zone() -> str:
    """The system's IANA zone: `$TZ`, else the `/etc/localtime` link, else UTC."""
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if "zoneinfo/" in link:
        candidates.append(link.split("zoneinfo/", 1)[1])
    for name in candidates:
        if name and _valid_zone(name):
            return name
    return "UTC"


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def ambiguous(ranked: Ranked) -> bool:
    """True when the best match is weak or the runner-up is within `AMBIGUOUS_GAP`."""
    if not ranked or ranked[0][1].confidence < MIN_CONFIDENCE:
        return True
    gap = ranked[0][1].confidence - ranked[1][1].confidence if len(ranked) > 1 else 1.0
    return gap <= AMBIGUOUS_GAP + 1e-9


def _candidates(ranked: Ranked) -> Ranked:
    plausible = [item for item in ranked if item[1].confidence > 0]
    return plausible or ranked


def choose(
    path: Path, connectors: Mapping[str, Connector], console: Console, ask: bool
) -> Connector:
    ranked = registry.rank(path, connectors)
    if not ambiguous(ranked):
        connector, result = ranked[0]
        console.print(
            f"Detected {connector.display_name} ({result.confidence:.0%}: {result.reason})",
            highlight=False,
            markup=False,
        )
        return connector
    candidates = _candidates(ranked)
    lines = [
        f"  {index}. {c.id:<16} {r.confidence:.2f}  {r.reason}"
        for index, (c, r) in enumerate(candidates, start=1)
    ]
    if not ask:
        raise DigError(
            f"cannot tell which connector reads {path}; candidates:\n"
            + "\n".join(lines)
            + "\nChoose one with --connector ID.",
            code=2,
        )
    console.print(f"Not sure which connector reads {path}:", highlight=False, markup=False)
    for line in lines:
        console.print(line, highlight=False, markup=False)
    while True:
        picked: int = typer.prompt("Connector number", type=int, default=1)
        if 1 <= picked <= len(candidates):
            return candidates[picked - 1][0]
        console.print(f"Pick a number from 1 to {len(candidates)}.")


def resolve_tz(store: Store, connector: Connector, flag: str | None) -> tuple[str, str]:
    """(zone, where it came from): --tz, else the last succeeded import, else the system zone."""
    if flag is not None:
        if not _valid_zone(flag):
            raise DigError(f"unknown time zone {flag!r}; use an IANA name like Europe/Bucharest", 2)
        return flag, "--tz"
    previous = store.query(
        "SELECT tz FROM imports WHERE connector = ? AND status = 'succeeded'"
        " ORDER BY started_at DESC LIMIT 1",
        [connector.id],
    ).column("tz")
    if len(previous) and _valid_zone(str(previous[0])):
        return str(previous[0]), f"the previous {connector.id} import"
    return local_zone(), "the system time zone"


def dig_export(
    path: Path,
    connector_id: str | None,
    tz: str | None,
    me: list[str],
    db: Path,
    ask: bool,
    console: Console,
) -> None:
    if not path.exists():
        raise DigError(f"no such file or folder: {path}")
    connectors = registry.discover()
    if not connectors:
        raise DigError("no connectors are installed")
    if connector_id is not None:
        if connector_id not in connectors:
            raise DigError(
                f"unknown connector {connector_id!r}; installed: {', '.join(connectors)}", 2
            )
        connector = connectors[connector_id]
    else:
        connector = choose(path, connectors, console, ask)

    if me:
        config.set_self_identities(connector.id, me)
    identities = me or config.self_identities(connector.id)

    with Store.open(db) as store:
        zone, origin = resolve_tz(store, connector, tz)
        console.print(f"Time zone: {zone} (from {origin}; override with --tz ZONE)")
        if identities:
            console.print(f"Treating {len(identities)} identities as you (change with --me)")
        ctx = ImportContext(export_root(path), ZoneInfo(zone), frozenset(identities))
        try:
            stats = run_import(store, connector, path, ctx)
            resolve(store)
        except Exception as error:
            if os.environ.get(DEBUG_ENV) == "1":
                raise
            raise DigError(
                f"{connector.id} could not read {path} ({type(error).__name__}: {error})"
            ) from None
    console.print(
        f"Imported with {connector.display_name}: {stats.seen:,} rows seen,"
        f" {stats.inserted:,} new → {db}",
        highlight=False,
        markup=False,
    )


def register(app: typer.Typer) -> None:
    @app.command()
    def dig(
        path: Annotated[Path, typer.Argument(help="An export file or folder")],
        connector: Annotated[
            str | None, typer.Option("--connector", "-c", help="Connector id; skips detection")
        ] = None,
        tz: Annotated[
            str | None, typer.Option(help="IANA time zone of the export's local times")
        ] = None,
        me: Annotated[
            list[str] | None,
            typer.Option(help="A name or number that is you; repeat; remembered per connector"),
        ] = None,
        db: Annotated[
            Path | None, typer.Option(help="Database (default: $SHERD_HOME/life.duckdb)")
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Never prompt; exit 2 when detection is unsure")
        ] = False,
    ) -> None:
        """Import an export: detect its format, then load it into your database."""
        console = Console(soft_wrap=True)
        try:
            dig_export(
                path,
                connector,
                tz,
                me or [],
                db or default_db_path(),
                interactive() and not yes,
                console,
            )
        except DigError as error:
            Console(stderr=True, soft_wrap=True).print(
                f"[red]error:[/] {escape(str(error))}", highlight=False
            )
            raise typer.Exit(error.code) from None
        except (OSError, ValueError, StoreError, registry.RegistryError, duckdb.Error) as error:
            if os.environ.get(DEBUG_ENV) == "1":
                raise
            Console(stderr=True, soft_wrap=True).print(
                f"[red]error:[/] {escape(str(error))}", highlight=False
            )
            raise typer.Exit(1) from None
