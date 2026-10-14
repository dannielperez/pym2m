"""Realtime events over the platform's Socket.IO 2.x endpoint (``pip install pym2m[live]``)."""

from pym2m.live.protocol import LiveProtocolError
from pym2m.live.session import (
    LiveSession,
    ReconnectPolicy,
    SocketClosed,
    TriggerEvent,
    WebSocketLike,
    recover_with_rest,
    websockets_connect,
)
from pym2m.live.state import (
    LiveStateCache,
    LiveStats,
    LiveUpdate,
    RecoveryRequest,
    UpdateKind,
    deep_merge,
)

__all__ = [
    "LiveProtocolError",
    "LiveSession",
    "LiveStateCache",
    "LiveStats",
    "LiveUpdate",
    "ReconnectPolicy",
    "RecoveryRequest",
    "SocketClosed",
    "TriggerEvent",
    "UpdateKind",
    "WebSocketLike",
    "deep_merge",
    "recover_with_rest",
    "websockets_connect",
]
