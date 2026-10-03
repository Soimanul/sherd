from importlib.metadata import version

import sherd_connectors


def test_version() -> None:
    assert sherd_connectors.__version__ == version("sherd-connectors") == "0.1.0"
