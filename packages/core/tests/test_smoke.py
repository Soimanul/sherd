from importlib.metadata import version

import sherd_core


def test_version() -> None:
    assert sherd_core.__version__ == version("sherd-core") == "0.1.0"
