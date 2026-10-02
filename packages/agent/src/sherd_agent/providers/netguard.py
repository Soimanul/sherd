"""`--offline`: refuse every socket connect except loopback and unix sockets."""

import ipaddress
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost"})


class OfflineError(ConnectionRefusedError):
    """A non-loopback connection was attempted while offline."""


def is_loopback(address: Any) -> bool:
    """True for unix-socket paths and loopback (host, port[, ...]) tuples."""
    if isinstance(address, str | bytes):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    host = host.split("%", 1)[0]
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@contextmanager
def offline_guard() -> Iterator[None]:
    """Patch `socket.socket` so non-loopback connects raise `OfflineError` inside the block."""
    original_connect: Callable[..., Any] = socket.socket.connect
    original_connect_ex: Callable[..., Any] = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        if not is_loopback(address):
            raise OfflineError("offline: non-loopback network access is blocked")
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if not is_loopback(address):
            raise OfflineError("offline: non-loopback network access is blocked")
        return original_connect_ex(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]  # the guard is a monkeypatch by design
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]  # see above
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]  # restore
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]  # restore
