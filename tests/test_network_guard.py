import socket
from pathlib import Path

import pytest
from pytest_socket import SocketConnectBlockedError


def test_public_connection_blocked() -> None:
    with socket.socket() as client, pytest.raises(SocketConnectBlockedError):
        client.connect(("1.1.1.1", 443))


def test_loopback_connection_allowed() -> None:
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.settimeout(1)
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        with accepted:
            client.sendall(b"ok")
            assert accepted.recv(2) == b"ok"


def test_unix_socket_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as client:
        listener.bind("socket")
        listener.listen(1)
        client.connect("socket")
        accepted, _ = listener.accept()
        accepted.close()
