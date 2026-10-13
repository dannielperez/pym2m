"""Sans-IO codec for the Socket.IO 2.x wire protocol (Engine.IO v3), WebSocket only.

The realtime server requires a Socket.IO **2.x** client with the WebSocket
transport. Socket.IO 2.x speaks Engine.IO protocol 3. Current releases of
python-socketio (5.x) only speak Engine.IO 4, and the 4.x line that spoke EIO3 is
no longer maintained. pym2m therefore implements the small framing layer itself
and runs it over the maintained ``websockets`` library.

Frame format (one WebSocket text message = one Engine.IO packet)::

    <eio type>[<sio type>[<namespace>,][<ack id>][<json>]]

Engine.IO types: 0 open, 1 close, 2 ping, 3 pong, 4 message.
Socket.IO types (inside a message): 0 connect, 1 disconnect, 2 event, 3 ack, 4 error.
In Engine.IO 3 the **client** sends pings and the server answers with pongs.
Binary packets (types 5/6) are not used by this API and are rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from pym2m.exceptions import M2MError

ENGINE_IO_VERSION = 3
SOCKET_PATH = "/socket.io/"
DEFAULT_NAMESPACE = "/socket"


class LiveProtocolError(M2MError):
    """A frame could not be decoded or violated the protocol."""


class EnginePacket(IntEnum):
    OPEN = 0
    CLOSE = 1
    PING = 2
    PONG = 3
    MESSAGE = 4
    UPGRADE = 5
    NOOP = 6


class SocketPacket(IntEnum):
    CONNECT = 0
    DISCONNECT = 1
    EVENT = 2
    ACK = 3
    ERROR = 4
    BINARY_EVENT = 5
    BINARY_ACK = 6


@dataclass(frozen=True, slots=True)
class OpenInfo:
    sid: str
    ping_interval_s: float
    ping_timeout_s: float


@dataclass(frozen=True, slots=True)
class Packet:
    type: SocketPacket
    namespace: str = "/"
    data: Any = None
    ack_id: int | None = None

    @property
    def event_name(self) -> str | None:
        if self.type is SocketPacket.EVENT and isinstance(self.data, list) and self.data:
            name = self.data[0]
            return name if isinstance(name, str) else None
        return None

    @property
    def event_args(self) -> list[Any]:
        if self.type is SocketPacket.EVENT and isinstance(self.data, list):
            return list(self.data[1:])
        if self.type is SocketPacket.ACK and isinstance(self.data, list):
            return list(self.data)
        return []


@dataclass(frozen=True, slots=True)
class Frame:
    kind: EnginePacket
    open_info: OpenInfo | None = None
    packet: Packet | None = None


def websocket_url(live_origin: str) -> str:
    """``https://host`` -> ``wss://host/socket.io/?EIO=3&transport=websocket``."""
    if not live_origin.startswith("https://"):
        raise LiveProtocolError("live origin must be https")
    host = live_origin[len("https://") :].rstrip("/")
    return f"wss://{host}{SOCKET_PATH}?EIO={ENGINE_IO_VERSION}&transport=websocket"


def encode_ping() -> str:
    return str(int(EnginePacket.PING))


def encode_packet(packet: Packet) -> str:
    out = [str(int(EnginePacket.MESSAGE)), str(int(packet.type))]
    if packet.type in (SocketPacket.BINARY_EVENT, SocketPacket.BINARY_ACK):
        raise LiveProtocolError("binary packets are not supported")
    if packet.namespace and packet.namespace != "/":
        out.append(packet.namespace + ",")
    if packet.ack_id is not None:
        out.append(str(packet.ack_id))
    if packet.data is not None:
        out.append(json.dumps(packet.data, separators=(",", ":")))
    return "".join(out)


def connect_namespace(namespace: str = DEFAULT_NAMESPACE) -> str:
    return encode_packet(Packet(SocketPacket.CONNECT, namespace))


def emit(
    event: str, *args: Any, namespace: str = DEFAULT_NAMESPACE, ack_id: int | None = None
) -> str:
    return encode_packet(Packet(SocketPacket.EVENT, namespace, [event, *args], ack_id))


def decode_frame(text: str) -> Frame:
    if not text:
        raise LiveProtocolError("empty frame")
    try:
        kind = EnginePacket(int(text[0]))
    except ValueError:
        raise LiveProtocolError("unknown Engine.IO packet type") from None
    body = text[1:]
    if kind is EnginePacket.OPEN:
        return Frame(kind, open_info=_decode_open(body))
    if kind is EnginePacket.MESSAGE:
        return Frame(kind, packet=decode_packet(body))
    return Frame(kind)


def _decode_open(body: str) -> OpenInfo:
    try:
        data = json.loads(body)
        return OpenInfo(
            sid=str(data["sid"]),
            ping_interval_s=float(data["pingInterval"]) / 1000.0,
            ping_timeout_s=float(data["pingTimeout"]) / 1000.0,
        )
    except (ValueError, KeyError, TypeError):
        raise LiveProtocolError("malformed Engine.IO open packet") from None


def decode_packet(body: str) -> Packet:
    if not body or not body[0].isdigit():
        raise LiveProtocolError("malformed Socket.IO packet")
    try:
        ptype = SocketPacket(int(body[0]))
    except ValueError:
        raise LiveProtocolError("unknown Socket.IO packet type") from None
    if ptype in (SocketPacket.BINARY_EVENT, SocketPacket.BINARY_ACK):
        raise LiveProtocolError("binary packets are not supported")
    pos = 1
    namespace = "/"
    if pos < len(body) and body[pos] == "/":
        comma = body.find(",", pos)
        if comma == -1:
            namespace, pos = body[pos:], len(body)
        else:
            namespace, pos = body[pos:comma], comma + 1
    start = pos
    while pos < len(body) and body[pos].isdigit():
        pos += 1
    ack_id = int(body[start:pos]) if pos > start else None
    data: Any = None
    if pos < len(body):
        try:
            data = json.loads(body[pos:])
        except ValueError:
            raise LiveProtocolError("Socket.IO packet payload is not JSON") from None
    return Packet(ptype, namespace, data, ack_id)
