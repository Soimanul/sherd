import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "socket",
    "ssl",
    "http.client",
    "urllib.request",
    "httpx",
    "requests",
    "aiohttp",
    "websockets",
)


def network_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return [
        name
        for name in imports
        if any(name == forbidden or name.startswith(forbidden + ".") for forbidden in FORBIDDEN)
    ]


def network_violations(root: Path) -> dict[str, list[str]]:
    providers = root / "packages/agent/src/sherd_agent/providers"
    return {
        str(path.relative_to(root)): network_imports(path)
        for path in root.glob("packages/*/src/**/*.py")
        if not path.is_relative_to(providers) and network_imports(path)
    }


def test_no_network_imports() -> None:
    assert not network_violations(ROOT)


@pytest.mark.parametrize(
    "statement",
    [
        "import socket",
        "import ssl",
        "import http.client",
        "from urllib import request",
        "import urllib.request as r",
        "from http import client",
        "from requests.adapters import HTTPAdapter",
        "import httpx",
        "import requests",
        "import aiohttp",
        "import websockets",
        "from http.client import HTTPSConnection",
        "import requests.sessions",
    ],
)
def test_checker_catches_violation(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "violation.py"
    path.write_text(statement)
    assert network_imports(path)


def test_checker_allows_unrelated_import(tmp_path: Path) -> None:
    path = tmp_path / "safe.py"
    path.write_text("import pathlib\nfrom . import socket\n")
    assert not network_imports(path)


@pytest.mark.parametrize(
    ("directory", "allowed"), [("providers", True), ("providers_extra", False)]
)
def test_checker_provider_boundary(tmp_path: Path, directory: str, allowed: bool) -> None:
    path = tmp_path / "packages/agent/src/sherd_agent" / directory / "adapter.py"
    path.parent.mkdir(parents=True)
    path.write_text("import httpx\n")
    assert bool(network_violations(tmp_path)) is not allowed
