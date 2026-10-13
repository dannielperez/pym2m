from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pym2m import (
    EventSource,
    PositionIssue,
    ResponseFormatError,
    UnsupportedResponseError,
)
from tests.conftest import FakeAPI, json_response

START = datetime(2025, 10, 9, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=6)


def raw_event(eid: int, minute: int, **extra):
    event = {
        "id": eid,
        "vid": 7,
        "device_id": 350000000000001,
        "event_time": (START + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S"),
        "system_time": (START + timedelta(minutes=minute, seconds=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ),
        "lat": 18.4,
        "lon": -66.05,
        "mph": 30,
        "valid_position": True,
        "io_ign": True,
        "hdop": 0.8,
        "sv": 9,
        "code": 1,
    }
    event.update(extra)
    return event


def test_rawdata_normalizes_and_sorts(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/rawdata",
        json_response(
            200,
            {
                "units": {"speed": "kph", "distance": "meter"},
                "events": [raw_event(2, 10), raw_event(1, 5, io_ign=False, valid_position=False)],
            },
        ),
    )
    events = make_client().rawdata([7], START, END)
    assert [e.event_id for e in events] == [1, 2]
    first = events[0]
    assert first.source is EventSource.REST_RAWDATA
    assert first.event_time == START + timedelta(minutes=5)
    assert first.received_time == START + timedelta(minutes=5, seconds=3)
    assert first.ignition is False
    assert first.fix.issue is PositionIssue.DEVICE_FLAGGED_INVALID
    assert events[1].fix.is_usable and events[1].speed.mph == 30
    assert events[1].fix.hdop == 0.8 and events[1].fix.accuracy_m is None
    assert first.device_online is None  # REST history carries no connection state
    params = api.requests[0].url.params
    assert params["entities"] == "7"
    assert params["from"] == "2025-10-09T00:00:00" and params["to"] == "2025-10-09T06:00:00"
    assert "valid_position" in params["fields"].split(",")


def test_rawdata_async_job_shape_is_unsupported(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/rawdata", json_response(200, {"job": 123}))
    with pytest.raises(UnsupportedResponseError):
        make_client().rawdata([7], START, END)


def test_rawdata_empty(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/rawdata", json_response(200, {"events": []}))
    assert make_client().rawdata([7], START, END) == []


def test_history_windows_and_entities_are_bounded(make_client) -> None:
    client = make_client()
    with pytest.raises(ValueError):
        client.rawdata([7], START, START + timedelta(days=2))
    with pytest.raises(ValueError):
        client.rawdata(range(1, 60), START, END)
    with pytest.raises(ValueError):
        client.trips([7], END, START)


def test_trips_list_and_units(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/trips",
        json_response(
            200,
            [
                {
                    "id": 11,
                    "start_time": "2025-10-09 01:00:00",
                    "distance": 1200,
                    "duration": 600,
                    "mean_speed": 7.2,
                    "moving": True,
                },
                {"id": 12, "start_time": "2025-10-09 02:00:00", "distance": 0, "moving": False},
            ],
        ),
    )
    trips = make_client().trips([7], START, END, fields=["distance", "duration", "start_time"])
    assert [t.id for t in trips] == [11, 12]
    assert trips[0].start_time == START + timedelta(hours=1)
    assert trips[0].distance == 1200 and trips[0].units["distance"] == "meter"
    assert trips[1].duration is None
    params = api.requests[0].url.params
    assert (
        params["distance"] == "meter" and params["speed"] == "kph" and params["time"] == "second"
    )


def test_trips_single_summary_object(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/trips", json_response(200, {"distance": 5, "duration": 9}))
    (trip,) = make_client().trips([7], START, END)
    assert trip.distance == 5 and trip.id is None


def test_counters(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/counters",
        json_response(
            200,
            {
                "units": {"distance": "meter", "time": "second"},
                "counters": [
                    {
                        "vid": 7,
                        "from": "2025-10-09T00:00:05",
                        "to": "2025-10-09T05:59:00",
                        "dev_dist": 52000,
                        "dev_ign": 7200,
                        "note": "text ignored",
                    }
                ],
            },
        ),
    )
    (totals,) = make_client().counters([7], START, END)
    assert totals.vehicle_id == 7
    assert totals.values == {"dev_dist": 52000.0, "dev_ign": 7200.0}
    assert totals.units["distance"] == "meter"


def test_counters_bad_shape(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/counters", json_response(200, []))
    with pytest.raises(ResponseFormatError):
        make_client().counters([7], START, END)


def test_vehicle_counters_raw(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/vehicles/7/counters", json_response(200, {"odometer": 1}))
    assert make_client().vehicle_counters(7) == {"odometer": 1}


def test_geofences_and_triggers(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/geofences",
        json_response(
            200, {"data": [{"id": 5, "name": "Depot", "groups": [10]}], "pages": 1, "total": 1}
        ),
    )
    api.add("GET", "/v2/api/triggers", json_response(200, [{"id": 9, "name": "Ignition on"}]))
    client = make_client()
    (fence,) = client.list_geofences(groups=[10], search={"name": "Depot"}).items
    assert fence.id == 5 and fence.groups == (10,)
    assert api.requests[0].url.params["search.name"] == "Depot"
    (trigger,) = client.list_triggers().items
    assert trigger.name == "Ignition on"


def test_alerts(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/alerts",
        json_response(
            200,
            [
                {
                    "tuuid": "u-1",
                    "trigger": 9,
                    "trigger_name": "Panic",
                    "object_id": 7,
                    "level": "critical",
                    "resolved": False,
                    "time": 1_760_000_000.5,
                    "fields_epoch": 1_760_000_000,
                    "fields_lat": 18.4,
                    "fields_lon": -66.05,
                }
            ],
        ),
    )
    (alert,) = make_client().alerts(datetime.now(UTC) - timedelta(hours=1), triggers=[9])
    assert alert.trigger_id == 9 and alert.object_id == 7 and alert.resolved is False
    assert alert.fix.is_usable


def test_reverse_geocode(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/reversegeo",
        json_response(
            200,
            {
                "address": "1 Example St",
                "zones": ["Depot", 3],
                "location_full": "Depot, 1 Example St",
            },
        ),
    )
    client = make_client()
    result = client.reverse_geocode(18.4655, -66.0701)
    assert result.address == "1 Example St" and result.zones == ("Depot",)
    assert api.requests[0].url.params["lat"] == "18.465500"
    with pytest.raises(ValueError):
        client.reverse_geocode(0, 0)


def test_list_groups(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        "/v2/api/groups",
        json_response(
            200,
            {
                "data": [
                    {
                        "id": 10,
                        "name": "Patrol fleet",
                        "company_name": "Example Co",
                        "vehicles": [7, 8],
                    },
                    {"id": 11, "name": "Spare"},
                ],
                "pages": 1,
                "total": 2,
            },
        ),
    )
    groups = make_client().list_groups().items
    assert [g.id for g in groups] == [10, 11]
    assert groups[0].vehicle_ids == (7, 8)
    assert groups[1].vehicle_ids == ()  # absent member list is not "empty"
    assert groups[1].company_name is None


def test_group_without_id_raises() -> None:
    from pym2m import Group

    with pytest.raises(ResponseFormatError):
        Group.from_api({"name": "x"})
