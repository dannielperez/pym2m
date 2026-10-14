"""The default ``websockets`` connector against a real local server (127.0.0.1 only).

The scripted ``FakeSocket`` in ``test_live.py`` raises ``SocketClosed`` itself,
so it cannot show what the real library does when a peer closes: ``websockets``
raises ``ConnectionClosed``, which is not an ``OSError``. These tests pin that
the connector maps it to ``SocketClosed`` so the session reconnects.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

websockets = pytest.importorskip("websockets")

from websockets.sync.server import serve  # noqa: E402

from pym2m import Secret  # noqa: E402
from pym2m.live import (  # noqa: E402
    LiveSession,
    ReconnectPolicy,
    SocketClosed,
    websockets_connect,
)

OPEN = "0" + json.dumps({"sid": "s", "upgrades": [], "pingInterval": 25000, "pingTimeout": 5000})


class LocalServer:
    """Answers the handshake, authenticates, then closes each connection."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
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
                if not self.hold:
                    conn.close()
                    return
            elif message == "2":
                conn.send("3")


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


def test_connected_reflects_authentication_not_thread_liveness() -> None:
    with LocalServer(hold=True) as server:
        stop = threading.Event()
        session = LiveSession(
            "https://live-1.pegasusgateway.com",
            site_url="https://tracking.example.test",
            token=lambda: Secret("synthetic-live-token"),
            on_update=lambda update: None,
            connect=lambda _url: websockets_connect(server.url),
            reconnect=ReconnectPolicy(base_delay_s=0, max_delay_s=0),
        )
        assert session.connected is False
        thread = threading.Thread(target=session.run, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not session.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.connected is True
        assert session.last_inbound_at is not None
        stop.set()
        thread.join(timeout=3)
        assert session.connected is False


def test_unreachable_endpoint_never_reports_connected() -> None:
    stop = threading.Event()
    session = LiveSession(
        "https://live-1.pegasusgateway.com",
        site_url="https://tracking.example.test",
        token=lambda: Secret("synthetic-live-token"),
        on_update=lambda update: None,
        connect=lambda _url: websockets_connect("ws://127.0.0.1:9/socket.io/"),
        reconnect=ReconnectPolicy(base_delay_s=0, max_delay_s=0),
    )
    session.run(stop, max_connections=2)
    assert session.connected is False
    assert session.last_inbound_at is None


def test_stop_interrupts_the_reconnect_backoff() -> None:
    stop = threading.Event()
    session = LiveSession(
        "https://live-1.pegasusgateway.com",
        site_url="https://tracking.example.test",
        token=lambda: Secret("synthetic-live-token"),
        on_update=lambda update: None,
        connect=lambda _url: websockets_connect("ws://127.0.0.1:9/socket.io/"),
        reconnect=ReconnectPolicy(base_delay_s=30, max_delay_s=30),
    )
    thread = threading.Thread(target=session.run, args=(stop,), daemon=True)
    thread.start()
    threading.Event().wait(0.3)
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
