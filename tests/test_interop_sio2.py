"""Wire-compatibility test against a real Socket.IO 2.5.0 server (synthetic data).

Opt-in: requires Node.js and ``npm install`` in ``tests/interop``. CI runs it in a
dedicated job. It proves protocol compatibility only; it is not evidence that any
live tracking platform accepts the session.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from pym2m import AuthenticationError, Secret
from pym2m.live import LiveSession, ReconnectPolicy
from pym2m.live.session import websockets_connect

INTEROP = Path(__file__).parent / "interop"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (INTEROP / "node_modules" / "socket.io").exists(),
    reason="Socket.IO 2.5.0 interop server not installed (cd tests/interop && npm install)",
)


@pytest.fixture
def server_port():
    proc = subprocess.Popen(
        ["node", str(INTEROP / "sio2_server.js")], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(proc.stdout.readline().strip())
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def run_session(port: int, token: str):
    def connect(url: str):
        return websockets_connect(
            url.replace("wss://live-1.pegasusgateway.com", f"ws://127.0.0.1:{port}")
        )

    updates, recoveries, stop = [], [], threading.Event()

    def on_update(update):
        updates.append(update)
        if len(updates) >= 4:
            stop.set()

    session = LiveSession(
        "https://live-1.pegasusgateway.com",
        site_url="https://tracking.example.test",
        token=lambda: Secret(token),
        on_update=on_update,
        on_recovery_needed=recoveries.append,
        connect=connect,
        reconnect=ReconnectPolicy(base_delay_s=0.1, max_delay_s=0.3),
    )
    session.subscribe([1])
    timer = threading.Timer(20, stop.set)
    timer.start()
    try:
        session.run(stop, max_connections=3)
    finally:
        timer.cancel()
    return session, updates, recoveries


def test_full_cycle_against_socketio_2_5_0(server_port) -> None:
    session, updates, recoveries = run_session(server_port, "good-token")
    events = [u.event for u in updates]
    assert [e.event_id for e in events] == [1001, 1501, 1002, 1502]
    # compact delta restored ignition / validity / hdop from cache
    assert events[1].ignition is True and events[1].fix.device_valid is True
    assert events[1].fix.hdop == 0.7
    # arrays replaced, not concatenated
    assert updates[1].state["device"]["groups"] == [9]
    # server-forced disconnect -> reconnect -> resubscribe -> recovery window
    (request,) = recoveries
    assert request.vehicle_ids == (1,)
    assert session.cache.stats.duplicates_dropped == 0


def test_bad_token_is_rejected_once(server_port) -> None:
    with pytest.raises(AuthenticationError):
        run_session(server_port, "bad-token")
