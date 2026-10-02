import socket
from pathlib import Path
from typing import Any

import pytest
from sherd_agent.providers.netguard import OfflineError, is_loopback, offline_guard


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        (("127.0.0.1", 80), True),
        (("127.8.9.10", 80), True),
        (("::1", 80, 0, 0), True),
        (("localhost", 11434), True),
        ("/tmp/sherd.sock", True),
        (b"/tmp/sherd.sock", True),
        (("1.1.1.1", 443), False),
        (("192.0.2.1", 80), False),
        (("2001:db8::1", 80, 0, 0), False),
        (("example.com", 443), False),
        (("::ffff:1.1.1.1", 443, 0, 0), False),
        (None, False),
    ],
)
def test_is_loopback(address: Any, allowed: bool) -> None:
    assert is_loopback(address) is allowed


def test_guard_blocks_public_and_allows_loopback_then_restores() -> None:
    original = socket.socket.connect
    with offline_guard():
        with socket.socket() as client, pytest.raises(OfflineError):
            client.connect(("192.0.2.1", 443))
        with socket.socket() as client, pytest.raises(OfflineError):
            client.connect_ex(("192.0.2.1", 443))
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            accepted.close()
    assert socket.socket.connect is original


def test_guard_allows_unix_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with (
        offline_guard(),
        socket.socket(socket.AF_UNIX) as listener,
        socket.socket(socket.AF_UNIX) as client,
    ):
        listener.bind("sock")
        listener.listen(1)
        client.connect("sock")
        listener.accept()[0].close()


def test_guard_restores_after_an_error() -> None:
    original = socket.socket.connect
    with pytest.raises(RuntimeError), offline_guard():
        raise RuntimeError("boom")
    assert socket.socket.connect is original
