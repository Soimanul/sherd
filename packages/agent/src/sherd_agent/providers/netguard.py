"""`--offline`: block remote connections and DNS; allow loopback and unix sockets."""

import ipaddress
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

LOOPBACK_NAMES = frozenset({"localhost"})


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

    def numeric_address(address: Any, family: int) -> Any:
        if isinstance(address, tuple) and address[0].lower() in ("localhost", b"localhost"):
            host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
            return (host, *address[1:])
        return address

    def connect(self: socket.socket, address: Any) -> Any:
        if not is_loopback(address):
            raise OfflineError("offline: non-loopback network access is blocked")
        return original_connect(self, numeric_address(address, self.family))

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if not is_loopback(address):
            raise OfflineError("offline: non-loopback network access is blocked")
        return original_connect_ex(self, numeric_address(address, self.family))

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]  # the guard is a monkeypatch by design
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]  # see above
    originals = {
        name: getattr(socket, name)
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection")
    }

    def guarded(name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            key = "address" if name == "create_connection" else "host"
            host = args[0] if args else kwargs.get(key)
            address = host if name == "create_connection" else (host, 0)
            if not is_loopback(address) or (
                name == "create_connection" and not isinstance(host, tuple)
            ):
                raise OfflineError("offline: non-loopback network access is blocked")
            if name == "create_connection":
                host = numeric_address(host, socket.AF_INET)
            else:
                if isinstance(host, bytes):
                    host = host.decode("ascii")
                assert isinstance(host, str)  # is_loopback validated the hostname
                if host.lower() == "localhost":
                    family = args[2] if len(args) > 2 else kwargs.get("family", 0)
                    host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
                if name == "gethostbyname_ex":
                    # The system version can perform reverse DNS on numeric input.
                    ipv4 = originals["gethostbyname"](host)
                    return (host, [], [ipv4])
                if name == "getaddrinfo":
                    # Even an allowed host must never trigger a DNS lookup.
                    if len(args) > 5:
                        args = (*args[:5], args[5] | socket.AI_NUMERICHOST, *args[6:])
                    else:
                        kwargs["flags"] = kwargs.get("flags", 0) | socket.AI_NUMERICHOST
            if args:
                args = (host, *args[1:])
            else:
                kwargs[key] = host
            return originals[name](*args, **kwargs)

        return call

    for name in originals:
        setattr(socket, name, guarded(name))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(socket, name, original)
        socket.socket.connect = original_connect  # type: ignore[method-assign]  # restore
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]  # restore
