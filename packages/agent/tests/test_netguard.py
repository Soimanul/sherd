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


@pytest.mark.parametrize(
    "name", ["getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"]
)
def test_offline_blocks_dns_before_resolver(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def resolver(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(socket, name, resolver)
    with offline_guard(), pytest.raises(OfflineError):
        getattr(socket, name)(
            ("synthetic.example.invalid", 80)
            if name == "create_connection"
            else "synthetic.example.invalid"
        )
    assert calls == []


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_http_client_offline_never_resolves_remote(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    client_module = importlib.import_module(module)
    calls: list[object] = []

    def resolver(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("DNS leaked")

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with (
        offline_guard(),
        client_module.Client(trust_env=False) as client,
        pytest.raises(client_module.ConnectError, match="offline"),
    ):
        client.get("http://synthetic.example.invalid")
    assert calls == []


def test_all_socket_functions_restored_after_exception() -> None:
    names = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection")
    originals = {name: getattr(socket, name) for name in names}
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def fail() -> None:
        with offline_guard():
            assert socket.getaddrinfo("127.8.9.10", 80)[0][4][0] == "127.8.9.10"
            assert socket.gethostbyname("localhost") == "127.0.0.1"
            assert socket.gethostbyname_ex("127.0.0.1")[2] == ["127.0.0.1"]
            assert socket.getaddrinfo("::1", 80)[0][4][0] == "::1"
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fail()
    assert all(getattr(socket, name) is original for name, original in originals.items())
    assert socket.socket.connect is connect
    assert socket.socket.connect_ex is connect_ex


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", b"localhost", "127.8.9.10", "::1"])
def test_allowed_dns_is_numeric_only(host: str | bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.getaddrinfo
    calls: list[object] = []

    def resolver(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("flags", 0) & socket.AI_NUMERICHOST
        calls.append(args[0] if args else kwargs["host"])
        return original(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with offline_guard():
        assert socket.getaddrinfo(host=host, port=80)
        assert socket.getaddrinfo("localhost", 80, family=socket.AF_INET6)[0][4][0] == "::1"
    assert calls == [
        "127.0.0.1" if host in ("localhost", "LOCALHOST", b"localhost") else host,
        "::1",
    ]


def test_create_connection_keyword_address_allows_loopback() -> None:
    with offline_guard(), socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(address=("localhost", listener.getsockname()[1])):
            listener.accept()[0].close()
        with pytest.raises(OfflineError):
            socket.create_connection(address=("synthetic.example.invalid", 80))
