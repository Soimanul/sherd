"""The semantic catalog: descriptions of every table and column, for the agent and the docs."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints

Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TableDoc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: Description
    columns: dict[str, Description]


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    description: Description
    conventions: list[Description]
    common_columns: dict[str, Description]
    """Provenance columns shared by every fact table (also listed under each such table)."""
    tables: dict[str, TableDoc]


def load_catalog(path: Path | None = None) -> Catalog:
    """Load `path`, or the catalog shipped with sherd_core."""
    if path is None:
        text = files("sherd_core").joinpath("catalog.yaml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return Catalog.model_validate(yaml.safe_load(text))
