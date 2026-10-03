from importlib.metadata import version

import sherd_agent


def test_version() -> None:
    assert sherd_agent.__version__ == version("sherd-agent") == "0.1.0"
