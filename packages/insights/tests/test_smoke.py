from importlib.metadata import version

import sherd_insights


def test_version() -> None:
    assert sherd_insights.__version__ == version("sherd-insights") == "0.1.0"
