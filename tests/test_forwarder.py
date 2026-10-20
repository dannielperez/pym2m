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


@pytest.mark.parametrize("count", [31, 125])
def test_large_fleets_and_duplicate_counts(count: int) -> None:
    items = [flat(i + 1, vid=i + 100) for i in range(count)]
    batch = parse_forwarder_body(json.dumps(items + items))
    assert len(batch.events) == count
    assert batch.duplicates == count
    assert not batch.rejected


@pytest.mark.parametrize("event_id", [None, True, -1, 0, "invalid", 1.5])
def test_requires_stable_positive_event_id(event_id) -> None:
    batch = parse_forwarder_body([flat(event_id)])
    assert not batch.events
    assert batch.rejected[0].reason == "missing or invalid event id"


def test_nested_rejection_keeps_normalized_id() -> None:
    batch = parse_forwarder_body([{"event": flat("12", event_time="invalid")}])
    assert batch.rejected[0].event_id == 12


def test_unknown_fields_preserved_without_repr_leakage() -> None:
    batch = parse_forwarder_body([flat(1, accessory={"synthetic": "private-value"})])
    assert batch.events[0].raw["accessory"] == {"synthetic": "private-value"}
    assert "private-value" not in repr(batch)


def test_predecoded_size_limit() -> None:
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body([flat(1, accessory="x" * 1000)], max_body_bytes=500)


@pytest.mark.parametrize("body", [b"[NaN]", [float("inf")], [{"bad": object()}]])
def test_non_json_values_fail_closed(body) -> None:
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body(body)


def test_nesting_limit_and_recursive_input_fail_closed() -> None:
    nested = []
    nested.append(nested)
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body(nested)
    with pytest.raises(ResponseFormatError):
        parse_forwarder_body("[" * 2000 + "]" * 2000)


@pytest.mark.parametrize("ids", [[True], [999], ["1"], [-1]])
def test_ack_rejects_ids_not_in_parsed_batch(ids) -> None:
    with pytest.raises(ValueError):
        parse_forwarder_body([flat(1)]).ack_body(ids)


@pytest.mark.parametrize("limits", [{"max_events": 0}, {"max_body_bytes": -1}])
def test_invalid_limits(limits) -> None:
    with pytest.raises(ValueError):
        parse_forwarder_body([], **limits)


def test_devicepkg_not_mistaken_for_location_event() -> None:
    batch = parse_forwarder_body([{"primary_id": 3, "device": {"online": True}}])
    assert not batch.events
    assert len(batch.rejected) == 1


def test_invalid_coordinates_remain_nonusable_not_fabricated() -> None:
    event = parse_forwarder_body([flat(1, lat=1000)]).events[0]
    assert not event.fix.is_usable


def test_conflicting_same_id_does_not_hide_another_vehicle() -> None:
    with pytest.raises(ResponseFormatError, match="conflicting duplicate"):
        parse_forwarder_body([flat(1, vid=3), flat(1, vid=4)])


def test_extreme_numeric_fields_rejected_without_escaping_batch() -> None:
    batch = parse_forwarder_body([flat(1, mph=10**400), flat(2), flat("1" * 5000)])
    assert [event.event_id for event in batch.events] == [2]
    assert len(batch.rejected) == 2
