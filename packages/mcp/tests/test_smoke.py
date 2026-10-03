from importlib.metadata import version

import sherd_mcp


def test_version() -> None:
    assert sherd_mcp.__version__ == version("sherd-mcp") == "0.1.0"
