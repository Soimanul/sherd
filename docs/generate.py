"""Generate reference pages from the installed registries before MkDocs reads docs."""

from pathlib import Path

from sherd_connectors.registry import discover as connectors
from sherd_insights.registry import discover as digs

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def on_pre_build(config: object) -> None:
    connector_dir = DOCS / "connectors"
    connector_dir.mkdir(exist_ok=True)
    listing = [
        "# Connectors",
        "",
        "Each format guide explains what to export and how sherd reads it.",
        "",
    ]
    for connector_id, connector in connectors().items():
        source = ROOT / "packages/connectors/src/sherd_connectors" / connector_id / "FORMAT.md"
        if not source.exists():
            raise FileNotFoundError(f"Missing format guide for {connector_id}: {source}")
        (connector_dir / f"{connector_id}.md").write_text(source.read_text())
        listing.append(f"- [{connector.display_name}]({connector_id}.md)")
    (connector_dir / "index.md").write_text("\n".join(listing) + "\n")

    dig_dir = DOCS / "digs"
    dig_dir.mkdir(exist_ok=True)
    lines = [
        "# Digs",
        "",
        "Digs are local, deterministic insights. Run `sherd show --list` "
        "to see what your data supports.",
        "",
    ]
    for dig_id, dig in digs().items():
        lines.extend(
            (
                f"## {dig.title}",
                "",
                f"`{dig_id}`",
                "",
                f"Requires: {', '.join(dig.requires) if dig.requires else 'any data'}.",
                "",
            )
        )
    (dig_dir / "index.md").write_text("\n".join(lines))
