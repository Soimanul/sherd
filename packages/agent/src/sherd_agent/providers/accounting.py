"""Count HTTP attempts at the transport boundary, including SDK retries.

Byte counts cover HTTP request/status lines, headers and raw body bytes, excluding
TLS and TCP framing. No content or header values are retained.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2

from sherd_agent.providers.base import Completion


@dataclass
class Accounting:
    requests: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def snapshot(self) -> Completion:
        return Completion(
            "",
            self.input_tokens,
            self.output_tokens,
            self.bytes_sent,
            self.bytes_received,
            self.requests,
        )


def headers_size(headers: Any) -> int:
    return sum(len(key) + len(value) + 4 for key, value in headers.raw) + 2


class CountingStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, counts: Accounting, *, sent: bool) -> None:
        self.stream = stream
        self.counts = counts
        self.sent = sent

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            if self.sent:
                self.counts.bytes_sent += len(chunk)
            else:
                self.counts.bytes_received += len(chunk)
            yield chunk

    def close(self) -> None:
        self.stream.close()


class CountingTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, counts: Accounting) -> None:
        self.transport = transport
        self.counts = counts

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.counts.requests += 1
        self.counts.bytes_sent += (
            len(request.method.encode())
            + len(request.url.raw_path)
            + len(b" HTTP/1.1\r\n")
            + 1
            + headers_size(request.headers)
        )
        assert isinstance(request.stream, httpx.SyncByteStream)
        request.stream = CountingStream(request.stream, self.counts, sent=True)
        # Cached content would let MockTransport/read() bypass the counted stream.
        # Count consumption, so a connection failure does not invent body bytes.
        if hasattr(request, "_content"):
            del request._content
        response = self.transport.handle_request(request)
        self.counts.bytes_received += (
            len(b"HTTP/1.1 ")
            + len(str(response.status_code))
            + 1
            + len(response.reason_phrase.encode())
            + 2
            + headers_size(response.headers)
        )
        if response.is_stream_consumed:
            self.counts.bytes_received += len(response.content)
        else:
            assert isinstance(response.stream, httpx.SyncByteStream)
            response.stream = CountingStream(response.stream, self.counts, sent=False)
        return response

    def close(self) -> None:
        self.transport.close()


class SDKCountingStream(httpx2.SyncByteStream):
    def __init__(self, stream: httpx2.SyncByteStream, counts: Accounting, *, sent: bool) -> None:
        self.stream = stream
        self.counts = counts
        self.sent = sent

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            if self.sent:
                self.counts.bytes_sent += len(chunk)
            else:
                self.counts.bytes_received += len(chunk)
            yield chunk

    def close(self) -> None:
        self.stream.close()


class SDKCountingTransport(httpx2.BaseTransport):
    def __init__(self, transport: httpx2.BaseTransport, counts: Accounting) -> None:
        self.transport = transport
        self.counts = counts

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.counts.requests += 1
        self.counts.bytes_sent += (
            len(request.method.encode())
            + len(request.url.raw_path)
            + len(b" HTTP/1.1\r\n")
            + 1
            + headers_size(request.headers)
        )
        assert isinstance(request.stream, httpx2.SyncByteStream)
        request.stream = SDKCountingStream(request.stream, self.counts, sent=True)
        # Cached content would let MockTransport/read() bypass the counted stream.
        # Count consumption, so a connection failure does not invent body bytes.
        if hasattr(request, "_content"):
            del request._content
        response = self.transport.handle_request(request)
        self.counts.bytes_received += (
            len(b"HTTP/1.1 ")
            + len(str(response.status_code))
            + 1
            + len(response.reason_phrase.encode())
            + 2
            + headers_size(response.headers)
        )
        if response.is_stream_consumed:
            self.counts.bytes_received += len(response.content)
        else:
            assert isinstance(response.stream, httpx2.SyncByteStream)
            response.stream = SDKCountingStream(response.stream, self.counts, sent=False)
        return response

    def close(self) -> None:
        self.transport.close()


def instrument_sdk_client(client: httpx2.Client, counts: Accounting) -> None:
    # httpx exposes transport injection at construction only. Preserve the supplied
    # client's timeout/auth/proxy configuration and wrap every routed transport.
    client._transport = SDKCountingTransport(client._transport, counts)
    client._mounts = {
        pattern: SDKCountingTransport(transport, counts) if transport is not None else None
        for pattern, transport in client._mounts.items()
    }
