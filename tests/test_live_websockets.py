"""The default ``websockets`` connector against a real local server (127.0.0.1 only).

The scripted ``FakeSocket`` in ``test_live.py`` raises ``SocketClosed`` itself,
so it cannot show what the real library does when a peer closes: ``websockets``
raises ``ConnectionClosed``, which is not an ``OSError``. These tests pin that
the connector maps it to ``SocketClosed`` so the session reconnects.
"""

from __future__ import annotations

import json
import threading

import pytest

websockets = pytest.importorskip("websockets")

from websockets.sync.server import serve  # noqa: E402

from pym2m import Secret  # noqa: E402
from pym2m.live import LiveSession, ReconnectPolicy, SocketClosed  # noqa: E402
from pym2m.live.session import websockets_connect  # noqa: E402

OPEN = "0" + json.dumps({"sid": "s", "upgrades": [], "pingInterval": 25000, "pingTimeout": 5000})


class LocalServer:
    """Answers the handshake, authenticates, then closes each connection."""

    def __init__(self) -> None:
        self.connections = 0
        self._server = serve(self._handle, "127.0.0.1", 0, compression=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> LocalServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/socket.io/?EIO=3&transport=websocket"

    def _handle(self, conn) -> None:
        self.connections += 1
        conn.send(OPEN)
        for message in conn:
            if message.startswith("40/socket"):
                conn.send("40/socket")
            elif message.startswith('42/socket,["authenticate"'):
                conn.send('42/socket,["_authenticated"]')
                conn.close()
                return


def test_peer_close_surfaces_as_socket_closed() -> None:
    with LocalServer() as server:
        ws = websockets_connect(server.url)
        assert ws.recv(timeout=2) == OPEN
        ws.send("40/socket")
        assert ws.recv(timeout=2) == "40/socket"
        ws.send('42/socket,["authenticate",{}]')
        assert ws.recv(timeout=2) == '42/socket,["_authenticated"]'
        with pytest.raises(SocketClosed):
            ws.recv(timeout=2)
        with pytest.raises(SocketClosed):
            ws.send("2")


def test_session_reconnects_after_the_server_closes() -> None:
    with LocalServer() as server:
        stop = threading.Event()
        session = LiveSession(
            "https://live-1.pegasusgateway.com",
            site_url="https://tracking.example.test",
            token=lambda: Secret("synthetic-live-token"),
            on_update=lambda update: None,
            connect=lambda _url: websockets_connect(server.url),
            reconnect=ReconnectPolicy(base_delay_s=0, max_delay_s=0),
            sleep=lambda s: None,
        )
        session.run(stop, max_connections=3)
        assert server.connections == 3
