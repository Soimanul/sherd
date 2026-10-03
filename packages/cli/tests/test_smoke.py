from importlib.metadata import version

import sherd_cli


def test_version() -> None:
    assert sherd_cli.__version__ == version("sherd-cli") == "0.1.0"
