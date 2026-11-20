"""HTTP sessions whose every connection leaves through one line, counting the bytes it sends.

Pinning:
- Every socket binds to the line's local address.
- On Linux it is also bound to the line's network device (SO_BINDTODEVICE), because Linux would
  otherwise route by destination and send everything out of the main default route. Unprivileged
  processes may do this on kernel 5.7 or later (Ubuntu 24.04 ships 6.8).
- Proxies are switched off: a proxy connection ignores the source address, so the upload would
  quietly fall back to the default line.

Counting: the socket is wrapped so each send is split into 256 KiB slices and counted as it goes,
which gives a smooth per-line speed even while the client sends a 64 MiB chunk in one call.
Before every slice the line is checked: if it was unplugged or got a new address, the send stops
with LineLostError, so the file moves to another line at once instead of waiting for a timeout.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

if TYPE_CHECKING:
    from src.network.lines import Line

_SLICE = 256 * 1024


class _CountingSocket:
    """Delegates everything to the real socket; counts and checks the line on every send."""

    def __init__(self, sock: Any, line: Line, generation: int) -> None:
        self._sock = sock
        self._line = line
        self._generation = generation

    def sendall(self, data: Any, *args: Any) -> None:
        view = memoryview(data).cast("B")
        for start in range(0, len(view), _SLICE):
            piece = view[start:start + _SLICE]
            self._line.check_open(self._generation)
            self._sock.sendall(piece, *args)
            self._line.add_sent(len(piece))

    def send(self, data: Any, *args: Any) -> int:
        self._line.check_open(self._generation)
        sent = self._sock.send(data, *args)
        self._line.add_sent(sent)
        return sent

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


def _connection_classes(line: Line, generation: int) -> tuple[type, type]:
    class LineHTTPConnection(HTTPConnection):
        def connect(self) -> None:
            line.check_open(generation)
            super().connect()
            self.sock = _CountingSocket(self.sock, line, generation)

    class LineHTTPSConnection(HTTPSConnection):
        def connect(self) -> None:
            line.check_open(generation)
            super().connect()
            self.sock = _CountingSocket(self.sock, line, generation)

    return LineHTTPConnection, LineHTTPSConnection


class LineAdapter(HTTPAdapter):
    """A requests adapter pinned to one line (its address, and on Linux its device)."""

    def __init__(self, line: Line, generation: int, pool_maxsize: int = 10) -> None:
        self._line = line
        self._generation = generation
        self._ip = line.ip
        self._device = line.device
        super().__init__(pool_connections=4, pool_maxsize=pool_maxsize, max_retries=0)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["source_address"] = (self._ip, 0)
        options = list(HTTPConnection.default_socket_options)
        if self._device and hasattr(socket, "SO_BINDTODEVICE"):
            options.append((socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                            self._device.encode()))
        kwargs["socket_options"] = options
        super().init_poolmanager(*args, **kwargs)
        plain, secure = _connection_classes(self._line, self._generation)
        self.poolmanager.pool_classes_by_scheme = {
            "http": type("LineHTTPPool", (HTTPConnectionPool,), {"ConnectionCls": plain}),
            "https": type("LineHTTPSPool", (HTTPSConnectionPool,), {"ConnectionCls": secure}),
        }


def pin(session: requests.Session, line: Line, generation: int,
        pool_maxsize: int = 10) -> requests.Session:
    """Route every request of `session` through `line`."""
    session.trust_env = False
    session.proxies = {}
    adapter = LineAdapter(line, generation, pool_maxsize)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def line_session(line: Line, pool_maxsize: int = 10) -> requests.Session:
    return pin(requests.Session(), line, line.generation, pool_maxsize)
