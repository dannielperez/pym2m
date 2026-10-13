from __future__ import annotations

import json

import pytest

from pym2m import EventSource, ResponseFormatError, parse_forwarder_body


def flat(eid: int, **extra):
    event = {
        "id": eid,
        "vid": 3,
        "device_id": 350000000000003,
        "event_time": "2025-10-09T12:00:00",
        "system_time": "2025-10-09T12:00:02.100",
        "lat": 18.2,
        "lon": -66.5,
        "mph": 25,
        "io_ign": True,
    }
    event.update(extra)
    return event


def test_flat_json_fwd_batch() -> None:
    batch = parse_forwarder_body(json.dumps([flat(1), flat(2, lat=None)]).encode())
    assert [e.event_id for e in batch.events] == [1, 2]
    assert batch.events[0].source is EventSource.FORWARDER
    assert batch.events[0].fix.is_usable and not batch.events[1].fix.is_usable
    assert batch.rejected == ()
    assert batch.ack_body() == []


def test_vehicle_events_shape_carries_online_state() -> None:
    item = {
        "primary_id": 3,
        "event": {"id": 5, "event_time": 1_760_000_000, "lat": 18.2, "lon": -66.5, "mph": 0},
        "device": {"connection": {"online": False}},
    }
    (event,) = parse_forwarder_body([item]).events
    assert event.vehicle_id == 3 and event.device_online is False


def test_duplicates_within_batch_are_collapsed() -> None:
    batch = parse_forwarder_body([flat(1), flat(1)])
    assert len(batch.events) == 1


def test_bad_items_are_rejected_individually() -> None:
    batch = parse_forwarder_body(
        [flat(1), "nope", flat(2, vid=None, device_id=None), flat(3, event_time="x")]
    )
    assert [e.event_id for e in batch.events] == [1]
    assert [r.index for r in batch.rejected] == [1, 2, 3]


@pytest.mark.parametrize("body", [b"not json", b'{"a": ', b'"string"', b"123"])
def test_unusable_bodies_raise(body) -> None:
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body(body)


def test_size_and_count_limits() -> None:
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body(b"[" + b" " * 100 + b"]", max_body_bytes=10)
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body([flat(i) for i in range(5)], max_events=4)


def test_ack_body_lists_failed_ids_once() -> None:
    batch = parse_forwarder_body([flat(1), flat(2)])
    assert batch.ack_body([2, 2, 1]) == [1, 2]
