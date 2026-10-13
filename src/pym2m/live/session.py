"""Realtime session: connect, authenticate, subscribe, reconnect, recover.

:class:`LiveSession` is a blocking loop meant to run in its own thread or process.
It owns one WebSocket and calls the consumer's callbacks synchronously from that
thread, so callbacks must be quick (hand work off to a queue).

Guarantees:

* The socket origin must pass :func:`pym2m.urls.validate_live_url`.
* At most ``max_entities`` (default 250) vehicles may be subscribed. Subscriptions
  are sent in chunks of ``chunk_size`` (default 100) with a short pause between
  chunks.
* After any disconnect the session reconnects with capped exponential backoff,
  re-authenticates, re-subscribes everything, and calls ``on_recovery_needed``
  with the window that may have been missed. Callers backfill it over REST, for
  example with :func:`recover_with_rest`.
* An authentication rejection is **fatal** (raised, not retried): repeated
  invalid connection attempts can get the caller's IP banned.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pym2m.exceptions import AuthenticationError, M2MError
from pym2m.live.protocol import (
    DEFAULT_NAMESPACE,
    EnginePacket,
    LiveProtocolError,
    OpenInfo,
    Packet,
    SocketPacket,
    connect_namespace,
    decode_frame,
    emit,
    encode_ping,
    websocket_url,
)
from pym2m.live.state import LiveStateCache, LiveUpdate, RecoveryRequest
from pym2m.models import EventSource, TelemetryEvent
from pym2m.normalize import to_int
from pym2m.redaction import Secret
from pym2m.urls import DEFAULT_LIVE_HOST_SUFFIXES, validate_live_url

logger = logging.getLogger("pym2m.live")

MAX_FRAME_BYTES = 1024 * 1024
VEHICLE_EVENTS = "vehicle-events"
TRIGGERS = "triggers"


class SocketClosed(M2MError):
    """The WebSocket closed or stopped answering."""


class WebSocketLike(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float) -> str:
        """Return one text message, or raise ``TimeoutError`` after ``timeout`` seconds."""
        ...

    def close(self) -> None: ...


def websockets_connect(url: str) -> WebSocketLike:
    """Default connector using the ``websockets`` package (``pip install pym2m[live]``)."""
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise M2MError("install pym2m[live] to use the realtime session") from exc

    ws = connect(url, open_timeout=10, close_timeout=5, max_size=MAX_FRAME_BYTES, compression=None)

    class _Adapter:
        def send(self, message: str) -> None:
            ws.send(message)

        def recv(self, timeout: float) -> str:
            message = ws.recv(timeout=timeout)
            if isinstance(message, bytes):
                raise LiveProtocolError("unexpected binary frame")
            return message

        def close(self) -> None:
            ws.close()

    return _Adapter()


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """A raw ``triggers`` namespace event. Its payload schema is not normalized."""

    object_id: int | None
    payload: Any = field(repr=False)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0

    def delay(self, attempt: int) -> float:
        return random.uniform(0, min(self.max_delay_s, self.base_delay_s * 2 ** min(attempt, 10)))


class LiveSession:
    def __init__(
        self,
        live_url: str,
        *,
        site_url: str,
        token: Callable[[], Secret],
        on_update: Callable[[LiveUpdate], None],
        on_trigger: Callable[[TriggerEvent], None] | None = None,
        on_recovery_needed: Callable[[RecoveryRequest], None] | None = None,
        connect: Callable[[str], WebSocketLike] = websockets_connect,
        cache: LiveStateCache | None = None,
        allowed_host_suffixes: tuple[str, ...] = DEFAULT_LIVE_HOST_SUFFIXES,
        namespace: str = DEFAULT_NAMESPACE,
        chunk_size: int = 100,
        chunk_delay_s: float = 0.05,
        handshake_timeout_s: float = 10.0,
        reconnect: ReconnectPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.origin = validate_live_url(live_url, allowed_host_suffixes=allowed_host_suffixes)
        self.site_url = site_url
        self._token = token
        self.on_update = on_update
        self.on_trigger = on_trigger
        self.on_recovery_needed = on_recovery_needed
        self._connect = connect
        self.cache = cache or LiveStateCache()
        self.namespace = namespace
        if not 1 <= chunk_size <= 100:
            raise ValueError("chunk_size must be between 1 and 100")
        self.chunk_size = chunk_size
        self.chunk_delay_s = chunk_delay_s
        self.handshake_timeout_s = handshake_timeout_s
        self.reconnect = reconnect or ReconnectPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._desired: set[int] = set()
        self._triggers = False
        self._ws: WebSocketLike | None = None
        self._ack_seq = 0
        self._disconnected_at: datetime | None = None

    # -- subscription management (thread-safe) -------------------------------------

    @property
    def subscribed(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._desired)

    def subscribe(self, vehicle_ids: Iterable[int]) -> None:
        ids = {v for v in vehicle_ids if isinstance(v, int) and not isinstance(v, bool) and v > 0}
        with self._lock:
            target = self._desired | ids
            if len(target) > self.cache.max_entities:
                raise ValueError(
                    f"cannot subscribe to more than {self.cache.max_entities} vehicles"
                )
            new = ids - self._desired
            self._desired = target
            ws = self._ws
        if ws is not None and new:
            self._send_listen(ws, sorted(new))

    def unsubscribe(self, vehicle_ids: Iterable[int]) -> None:
        ids = set(vehicle_ids)
        with self._lock:
            removed = ids & self._desired
            self._desired -= removed
            ws = self._ws
        self.cache.forget(removed)
        if ws is not None and removed:
            for chunk in _chunks(sorted(removed), self.chunk_size):
                ws.send(
                    emit(
                        "stop",
                        {"namespace": VEHICLE_EVENTS, "objects": chunk},
                        namespace=self.namespace,
                    )
                )

    def subscribe_triggers(self) -> None:
        with self._lock:
            self._triggers = True
            ws = self._ws
        if ws is not None:
            ws.send(
                emit("listen", {"namespace": TRIGGERS, "objects": "all"}, namespace=self.namespace)
            )

    # -- main loop -------------------------------------------------------------------

    def run(self, stop: threading.Event, *, max_connections: int | None = None) -> None:
        """Connect and process events until ``stop`` is set.

        ``max_connections`` bounds the number of connection attempts (for tests and
        supervised workers); ``None`` reconnects indefinitely with backoff.
        """
        attempt = 0
        connections = 0
        while not stop.is_set():
            if max_connections is not None and connections >= max_connections:
                return
            connections += 1
            try:
                self._run_once(stop)
                attempt = 0
            except AuthenticationError:
                raise
            except (SocketClosed, LiveProtocolError, OSError, TimeoutError) as exc:
                logger.warning("pym2m.live: connection lost (%s)", type(exc).__name__)
            finally:
                with self._lock:
                    ws, self._ws = self._ws, None
                if ws is not None:
                    # Closing an already-dead socket must never mask the real error.
                    with contextlib.suppress(Exception):
                        ws.close()
                self._disconnected_at = self._disconnected_at or datetime.now(UTC)
            if stop.is_set():
                return
            attempt += 1
            self._sleep(self.reconnect.delay(attempt))

    def _run_once(self, stop: threading.Event) -> None:
        ws = self._connect(websocket_url(self.origin))
        with self._lock:
            self._ws = ws
        info = self._handshake(ws)
        self._authenticate(ws)
        with self._lock:
            desired = sorted(self._desired)
            triggers = self._triggers
        reconnected_after = self._disconnected_at
        self._disconnected_at = None
        if desired:
            self._send_listen(ws, desired)
        if triggers:
            ws.send(
                emit("listen", {"namespace": TRIGGERS, "objects": "all"}, namespace=self.namespace)
            )
        if reconnected_after is not None and self.on_recovery_needed is not None:
            request = self.cache.recovery_request(desired, disconnected_at=reconnected_after)
            if request is not None:
                self.on_recovery_needed(request)
        self._read_loop(ws, stop, info.ping_interval_s, info.ping_timeout_s)

    def _handshake(self, ws: WebSocketLike) -> OpenInfo:
        deadline = self._monotonic() + self.handshake_timeout_s
        opening = decode_frame(self._recv(ws, deadline))
        if opening.kind is not EnginePacket.OPEN or opening.open_info is None:
            raise LiveProtocolError("expected Engine.IO open packet")
        ws.send(connect_namespace(self.namespace))
        while True:
            packet = decode_frame(self._recv(ws, deadline)).packet
            if packet is None or packet.namespace != self.namespace:
                continue
            if packet.type is SocketPacket.CONNECT:
                return opening.open_info
            if packet.type is SocketPacket.ERROR:
                raise LiveProtocolError("namespace connection refused")

    def _authenticate(self, ws: WebSocketLike) -> None:
        token = self._token()
        ws.send(
            emit(
                "authenticate",
                {"pegasus": self.site_url, "auth": token.reveal()},
                namespace=self.namespace,
            )
        )
        deadline = self._monotonic() + self.handshake_timeout_s
        while True:
            packet = decode_frame(self._recv(ws, deadline)).packet
            if packet is None or packet.namespace != self.namespace:
                continue
            if packet.event_name == "_authenticated":
                logger.info("pym2m.live: authenticated")
                return
            if packet.event_name == "_error":
                # The message may echo request content; never surface it verbatim.
                raise AuthenticationError("live server rejected authentication")

    def _read_loop(
        self, ws: WebSocketLike, stop: threading.Event, ping_interval: float, ping_timeout: float
    ) -> None:
        next_ping = self._monotonic() + ping_interval
        last_inbound = self._monotonic()
        while not stop.is_set():
            now = self._monotonic()
            if now >= next_ping:
                ws.send(encode_ping())
                next_ping = now + ping_interval
            if now - last_inbound > ping_interval + ping_timeout:
                raise SocketClosed("no traffic within ping interval + timeout")
            try:
                message = ws.recv(timeout=max(0.05, min(1.0, next_ping - now)))
            except TimeoutError:
                continue
            last_inbound = self._monotonic()
            if len(message) > MAX_FRAME_BYTES:
                logger.warning("pym2m.live: dropped oversize frame")
                continue
            try:
                frame = decode_frame(message)
            except LiveProtocolError:
                logger.warning("pym2m.live: dropped undecodable frame")
                continue
            if frame.kind is EnginePacket.CLOSE:
                raise SocketClosed("server closed the session")
            if frame.packet is not None:
                self._dispatch(frame.packet)

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, packet: Packet) -> None:
        if packet.namespace != self.namespace:
            return
        if packet.type is SocketPacket.DISCONNECT:
            raise SocketClosed("server disconnected the namespace")
        if packet.type is SocketPacket.ACK:
            # Cache callback for a listen request: a list of envelopes.
            for arg in packet.event_args:
                for envelope in arg if isinstance(arg, list) else [arg]:
                    self._handle_envelope(envelope)
            return
        if packet.event_name == "events":
            for arg in packet.event_args:
                for envelope in arg if isinstance(arg, list) else [arg]:
                    self._handle_envelope(envelope)

    def _handle_envelope(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            return
        namespace = envelope.get("namespace")
        if namespace == TRIGGERS:
            if self.on_trigger is not None:
                self.on_trigger(
                    TriggerEvent(to_int(envelope.get("object")), envelope.get("payload"))
                )
            return
        if namespace != VEHICLE_EVENTS:
            return
        payload = envelope.get("payload")
        if isinstance(payload, dict) and payload.get("primary_id") is None:
            payload = {**payload, "primary_id": envelope.get("object")}
        with self._lock:
            wanted = self._desired
        if to_int(envelope.get("object")) not in wanted:
            return
        update = self.cache.apply(payload)
        if update is not None:
            self.on_update(update)

    # -- helpers -----------------------------------------------------------------------

    def _send_listen(self, ws: WebSocketLike, ids: list[int]) -> None:
        for index, chunk in enumerate(_chunks(ids, self.chunk_size)):
            if index:
                self._sleep(self.chunk_delay_s)
            self._ack_seq += 1
            ws.send(
                emit(
                    "listen",
                    {"namespace": VEHICLE_EVENTS, "objects": chunk, "compact": True},
                    namespace=self.namespace,
                    ack_id=self._ack_seq,
                )
            )

    def _recv(self, ws: WebSocketLike, deadline: float) -> str:
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SocketClosed("handshake timed out")
            try:
                return ws.recv(timeout=remaining)
            except TimeoutError:
                continue


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def recover_with_rest(
    client: Any,
    request: RecoveryRequest,
    cache: LiveStateCache,
    *,
    max_window: timedelta | None = None,
) -> list[TelemetryEvent]:
    """Backfill a recovery window from REST rawdata and return only unseen events.

    ``client`` is an :class:`pym2m.M2MClient`. The window is walked in slices no
    longer than the client's ``max_history_window``.
    """
    window = max_window or client.max_history_window
    events: list[TelemetryEvent] = []
    start = request.since
    while start < request.until:
        end = min(start + window, request.until)
        events.extend(
            client.rawdata(request.vehicle_ids, start, end, source=EventSource.REST_RECOVERY)
        )
        start = end
    return cache.ingest_recovered(events)
