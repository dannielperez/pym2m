from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest

from pym2m import AuthenticationError, EventSource, Secret, TelemetryEvent
from pym2m.exceptions import UnsafeURLError
from pym2m.live import (
    LiveProtocolError,
    LiveSession,
    LiveStateCache,
    ReconnectPolicy,
    UpdateKind,
    deep_merge,
    recover_with_rest,
)
from pym2m.live.protocol import (
    EnginePacket,
    Packet,
    SocketPacket,
    decode_frame,
    decode_packet,
    emit,
    encode_packet,
    websocket_url,
)
from tests.conftest import FakeAPI, json_response

T0 = 1_760_000_000

# -- protocol codec ----------------------------------------------------------------


def test_encode_namespace_event_with_ack() -> None:
    frame = emit("listen", {"namespace": "vehicle-events", "objects": [1]}, ack_id=7)
    assert frame == '42/socket,7["listen",{"namespace":"vehicle-events","objects":[1]}]'
    assert encode_packet(Packet(SocketPacket.CONNECT, "/socket")) == "40/socket,"
    assert encode_packet(Packet(SocketPacket.CONNECT, "/")) == "40"


def test_decode_frames() -> None:
    opened = decode_frame('0{"sid":"abc","pingInterval":25000,"pingTimeout":5000,"upgrades":[]}')
    assert opened.kind is EnginePacket.OPEN
    assert opened.open_info.ping_interval_s == 25 and opened.open_info.ping_timeout_s == 5
    assert decode_frame("3").kind is EnginePacket.PONG
    event = decode_frame('42/socket,["events",{"namespace":"vehicle-events","object":1}]').packet
    assert event.namespace == "/socket" and event.event_name == "events"
    assert event.event_args == [{"namespace": "vehicle-events", "object": 1}]
    ack = decode_packet('3/socket,12[[{"a":1}]]')
    assert ack.type is SocketPacket.ACK and ack.ack_id == 12 and ack.event_args == [[{"a": 1}]]
    assert decode_packet("0/socket").namespace == "/socket"
    assert decode_packet('2["x"]').namespace == "/"


@pytest.mark.parametrize(
    "bad", ["", "9", "4", "4x", "42/socket,{bad", "45/socket,1-[]", '0{"sid":1}']
)
def test_decode_rejects_malformed(bad) -> None:
    with pytest.raises(LiveProtocolError):
        decode_frame(bad)


def test_websocket_url() -> None:
    assert websocket_url("https://live.pegasusgateway.com") == (
        "wss://live.pegasusgateway.com/socket.io/?EIO=3&transport=websocket"
    )


# -- merge semantics -----------------------------------------------------------------


def test_deep_merge_replaces_arrays_and_merges_objects() -> None:
    base = {
        "device": {"ios_state": {"io_in1": True, "io_in2": False}, "groups": [1, 2, 3]},
        "a": 1,
    }
    update = {"device": {"ios_state": {"io_in2": True}, "groups": [9]}, "a": None}
    merged = deep_merge(base, update)
    assert merged == {
        "device": {"ios_state": {"io_in1": True, "io_in2": True}, "groups": [9]},
        "a": None,
    }
    assert base["device"]["groups"] == [1, 2, 3]  # inputs untouched


def full_payload(vid: int, eid: int, t: int, **event):
    ev = {
        "id": eid,
        "event_time": t,
        "lat": 18.4,
        "lon": -66.05,
        "mph": 20,
        "io_ign": True,
        "valid_position": True,
        "code": 1,
    }
    ev.update(event)
    return {
        "primary_id": vid,
        "updates": ["device", "event"],
        "event": ev,
        "device": {"connection": {"online": True}, "latest": {"data": {"hdop": {"value": 0.9}}}},
        "primary": {"name": f"Unit {vid}"},
    }


def test_compact_event_reconstruction_and_restore() -> None:
    cache = LiveStateCache()
    first = cache.apply(full_payload(1, 100, T0))
    assert first.kind is UpdateKind.EVENT and first.event.ignition is True
    # Compact update: new event with only changed keys; restore io_ign (False-safe) and hdop.
    compact = {
        "primary_id": 1,
        "updates": ["event"],
        "event": {"id": 101, "event_time": T0 + 30, "lat": 18.41, "lon": -66.06},
        "_event_restore": ["io_ign", "valid_position", "hdop"],
    }
    second = cache.apply(compact)
    assert second.event.event_id == 101
    assert second.event.fix.lat == 18.41
    assert second.event.ignition is True  # restored from previous event
    assert second.event.fix.device_valid is True
    assert second.event.fix.hdop == 0.9  # restored from device.latest.data
    assert second.event.speed is None  # mph was not restored, so it is unknown, not stale
    assert second.device_online is True


def test_falsy_values_survive_restore() -> None:
    cache = LiveStateCache()
    cache.apply(full_payload(1, 100, T0, io_ign=False, mph=0))
    update = cache.apply(
        {
            "primary_id": 1,
            "updates": ["event"],
            "event": {"id": 101, "event_time": T0 + 5},
            "_event_restore": ["io_ign", "mph"],
        }
    )
    assert update.event.ignition is False
    assert update.event.speed.mph == 0


def test_device_only_update() -> None:
    cache = LiveStateCache()
    cache.apply(full_payload(1, 100, T0))
    update = cache.apply(
        {"primary_id": 1, "updates": ["device"], "device": {"connection": {"online": False}}}
    )
    assert (
        update.kind is UpdateKind.DEVICE and update.device_online is False and update.event is None
    )
    assert update.state["device"]["latest"]["data"]["hdop"]["value"] == 0.9


def test_duplicate_payload_is_not_redelivered() -> None:
    cache = LiveStateCache()
    assert cache.apply(full_payload(1, 100, T0)) is not None
    assert cache.apply(full_payload(1, 100, T0)) is None
    assert cache.stats.duplicates_dropped == 1


def test_out_of_order_event_does_not_replace_newer_state() -> None:
    cache = LiveStateCache()
    cache.apply(full_payload(1, 101, T0 + 60, lat=18.5))
    late = cache.apply(full_payload(1, 100, T0, lat=18.1))
    assert late.out_of_order is True
    assert cache.snapshot(1)["event"]["lat"] == 18.5
    assert cache.newest_event_time(1) == datetime.fromtimestamp(T0 + 60, tz=UTC)


def test_bounded_entities_evict_oldest() -> None:
    cache = LiveStateCache(max_entities=2)
    for vid in (1, 2, 3):
        cache.apply(full_payload(vid, vid, T0))
    assert len(cache) == 2 and cache.snapshot(1) is None and cache.stats.evicted == 1


def test_bounded_dedup_window() -> None:
    cache = LiveStateCache(dedup_window=2)
    for eid in (1, 2, 3):
        cache.apply(full_payload(1, eid, T0 + eid))
    track = cache._tracks[1]
    assert len(track.recent_keys) == 2


def test_rejects_payload_without_entity() -> None:
    cache = LiveStateCache()
    assert cache.apply({"updates": ["event"], "event": {}}) is None
    assert cache.apply("junk") is None
    assert cache.stats.rejected == 2


def test_stale_detection_means_unknown_not_stopped() -> None:
    now = [0.0]
    cache = LiveStateCache(monotonic=lambda: now[0])
    cache.apply(full_payload(1, 1, T0))
    now[0] = 30
    cache.apply(full_payload(2, 2, T0))
    now[0] = 100
    assert cache.stale_vehicles(max_silence_s=80) == [1]


# -- recovery ------------------------------------------------------------------------


def test_recovery_request_and_rest_ingest(api: FakeAPI, make_client) -> None:
    cache = LiveStateCache()
    cache.apply(full_payload(7, 100, T0))
    disconnected = datetime.fromtimestamp(T0 + 120, tz=UTC)
    now = datetime.fromtimestamp(T0 + 600, tz=UTC)
    request = cache.recovery_request([7], disconnected_at=disconnected, now=now)
    assert request.since == datetime.fromtimestamp(T0, tz=UTC) and request.until == now

    def iso(t):
        return datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")

    api.add(
        "GET",
        "/v2/api/rawdata",
        json_response(
            200,
            {
                "events": [
                    {
                        "id": 100,
                        "vid": 7,
                        "event_time": iso(T0),
                        "lat": 18.4,
                        "lon": -66.05,
                    },  # already seen
                    {"id": 101, "vid": 7, "event_time": iso(T0 + 200), "lat": 18.5, "lon": -66.05},
                ]
            },
        ),
    )
    fresh = recover_with_rest(make_client(), request, cache)
    assert [e.event_id for e in fresh] == [101]
    assert fresh[0].source is EventSource.REST_RECOVERY
    assert cache.newest_event_time(7) == datetime.fromtimestamp(T0 + 200, tz=UTC)
    # The same event arriving live afterwards is not delivered again.
    assert cache.apply(full_payload(7, 101, T0 + 200)) is None


# -- session with a scripted socket ------------------------------------------------------


class FakeSocket:
    def __init__(self, inbound: list[str], *, on_exhausted: str, stop: threading.Event) -> None:
        self.inbound = list(inbound)
        self.sent: list[str] = []
        self.on_exhausted = on_exhausted
        self.stop = stop
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float) -> str:
        if self.inbound:
            return self.inbound.pop(0)
        if self.on_exhausted == "drop":
            raise OSError("connection reset")
        self.stop.set()
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


OPEN = '0{"sid":"s1","pingInterval":25000,"pingTimeout":5000}'
NS_OK = "40/socket,"
AUTH_OK = '42/socket,["_authenticated",{"vehicles":[1,2]}]'


def envelope(payload, vid=1):
    return "42/socket," + json.dumps(
        ["events", {"namespace": "vehicle-events", "object": vid, "payload": payload}]
    )


def make_session(scripts, stop, **kwargs):
    sockets: list[FakeSocket] = []

    def connect(url: str) -> FakeSocket:
        assert url == "wss://live-1.pegasusgateway.com/socket.io/?EIO=3&transport=websocket"
        inbound, mode = scripts.pop(0)
        sock = FakeSocket(inbound, on_exhausted=mode, stop=stop)
        sockets.append(sock)
        return sock

    updates, recoveries = [], []
    session = LiveSession(
        "https://live-1.pegasusgateway.com",
        site_url="https://tracking.example.test",
        token=lambda: Secret("live-token-xyz"),
        on_update=updates.append,
        on_recovery_needed=recoveries.append,
        connect=connect,
        sleep=lambda s: None,
        reconnect=ReconnectPolicy(base_delay_s=0, max_delay_s=0),
        **kwargs,
    )
    return session, sockets, updates, recoveries


def test_session_authenticates_subscribes_in_chunks_and_delivers() -> None:
    stop = threading.Event()
    session, sockets, updates, _ = make_session(
        [
            (
                [
                    OPEN,
                    NS_OK,
                    AUTH_OK,
                    envelope(full_payload(1, 100, T0)),
                    envelope(full_payload(1, 100, T0)),
                ],
                "stop",
            )
        ],
        stop,
    )
    session.subscribe(range(1, 151))
    session.run(stop, max_connections=1)
    sent = sockets[0].sent
    assert sent[0] == "40/socket,"
    auth = json.loads(sent[1][len("42/socket,") :])
    assert auth == [
        "authenticate",
        {"pegasus": "https://tracking.example.test", "auth": "live-token-xyz"},
    ]
    listens = [json.loads(m[m.index("[") :]) for m in sent[2:] if '"listen"' in m]
    assert [len(item[1]["objects"]) for item in listens] == [100, 50]
    assert all(item[1]["compact"] is True for item in listens)
    assert len(updates) == 1  # duplicate suppressed
    assert sockets[0].closed


def test_session_reconnects_resubscribes_and_requests_recovery() -> None:
    stop = threading.Event()
    session, sockets, updates, recoveries = make_session(
        [
            ([OPEN, NS_OK, AUTH_OK, envelope(full_payload(1, 100, T0))], "drop"),
            ([OPEN, NS_OK, AUTH_OK, envelope(full_payload(1, 101, T0 + 60))], "stop"),
        ],
        stop,
    )
    session.subscribe([1])
    session.run(stop, max_connections=2)
    assert len(sockets) == 2
    assert any('"listen"' in m for m in sockets[1].sent)  # resubscribed
    assert [u.event.event_id for u in updates] == [100, 101]
    (request,) = recoveries
    assert request.vehicle_ids == (1,)
    assert request.since == datetime.fromtimestamp(T0, tz=UTC)


def test_session_auth_rejection_is_fatal_and_does_not_leak_token() -> None:
    stop = threading.Event()
    session, sockets, _, _ = make_session(
        [([OPEN, NS_OK, '42/socket,["_error","bad token live-token-xyz"]'], "stop")], stop
    )
    with pytest.raises(AuthenticationError) as excinfo:
        session.run(stop, max_connections=3)
    assert "live-token-xyz" not in str(excinfo.value)
    assert len(sockets) == 1  # no retry storm


def test_session_ignores_unsubscribed_and_foreign_events() -> None:
    stop = threading.Event()
    session, _, updates, _ = make_session(
        [
            (
                [
                    OPEN,
                    NS_OK,
                    AUTH_OK,
                    envelope(full_payload(9, 1, T0), vid=9),
                    '42/other,["events",{}]',
                    "4junk",
                ],
                "stop",
            )
        ],
        stop,
    )
    session.subscribe([1])
    session.run(stop, max_connections=1)
    assert updates == []


def test_session_cache_callback_ack_is_processed() -> None:
    stop = threading.Event()
    ack = "43/socket,1" + json.dumps(
        [[{"namespace": "vehicle-events", "object": 1, "payload": full_payload(1, 5, T0)}]]
    )
    session, _, updates, _ = make_session([([OPEN, NS_OK, AUTH_OK, ack], "stop")], stop)
    session.subscribe([1])
    session.run(stop, max_connections=1)
    assert [u.event.event_id for u in updates] == [5]


def test_session_subscription_limit_and_url_validation() -> None:
    stop = threading.Event()
    session, *_ = make_session([], stop)
    with pytest.raises(ValueError):
        session.subscribe(range(1, 252))
    with pytest.raises(UnsafeURLError):
        LiveSession(
            "https://evil.example", site_url="x", token=lambda: Secret("t"), on_update=print
        )


def test_unsubscribe_sends_stop_and_forgets_state() -> None:
    stop = threading.Event()
    session, sockets, _, _ = make_session([([OPEN, NS_OK, AUTH_OK], "stop")], stop)
    session.subscribe([1, 2])
    session.run(stop, max_connections=1)
    sock = sockets[0]
    session._ws = sock
    session.unsubscribe([2])
    assert '"stop"' in sock.sent[-1] and session.subscribed == frozenset({1})


def test_telemetry_event_dedup_key_without_id() -> None:
    a = TelemetryEvent.from_fields(
        {"vid": 1, "event_time": T0, "lat": 1, "lon": 1, "code": 2}, source=EventSource.LIVE
    )
    b = TelemetryEvent.from_fields(
        {"vid": 1, "event_time": T0, "lat": 1, "lon": 1, "code": 2}, source=EventSource.FORWARDER
    )
    assert a.dedup_key == b.dedup_key
    assert (
        a.dedup_key
        != TelemetryEvent.from_fields(
            {"vid": 1, "event_time": T0 + 1, "lat": 1, "lon": 1}, source=EventSource.LIVE
        ).dedup_key
    )
