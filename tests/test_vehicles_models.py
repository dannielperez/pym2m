from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pym2m import (
    Device,
    GpsFix,
    PositionIssue,
    ResponseFormatError,
    Secret,
    Speed,
    SpeedUnit,
    Vehicle,
    validate_live_url,
)
from pym2m.exceptions import UnsafeURLError
from pym2m.normalize import format_api_datetime, parse_timestamp, to_bool
from pym2m.params import field_list, id_list, search_params, time_window
from pym2m.redaction import redact_text
from tests.conftest import FakeAPI, json_response, vehicle_payload

VEHICLES = "/v2/api/vehicles"


def page(data, *, page_no=1, pages=1, total=None):
    return json_response(
        200,
        {
            "set": len(data),
            "total": total or len(data),
            "page": page_no,
            "pages": pages,
            "data": data,
        },
    )


# -- vehicles --------------------------------------------------------------------


def test_list_vehicles_parses_identity_and_latest_location(api: FakeAPI, make_client) -> None:
    api.add("GET", VEHICLES, page([vehicle_payload(7)]))
    result = make_client().list_vehicles(groups=[10])
    assert not result.truncated
    (vehicle,) = result.items
    assert vehicle.id == 7
    assert vehicle.device_imei == 350000000000001
    assert vehicle.info.license_plate == "TEST-001"
    loc = vehicle.latest_location
    assert loc is not None and loc.fix.is_usable
    assert (loc.fix.lat, loc.fix.lon) == (18.40, -66.05)
    assert loc.speed == Speed(10.0, SpeedUnit.MPH)
    assert loc.speed.kph == pytest.approx(16.09344)
    assert loc.event_time == datetime.fromtimestamp(1_760_000_000, tz=UTC)
    assert loc.received_time is not None and loc.received_time > loc.event_time
    assert loc.fix.accuracy_m is None  # never invented
    assert vehicle.connection is not None and vehicle.connection.online is True
    assert vehicle.raw["name"] == "Unit 7"
    params = api.requests[0].url.params
    assert params["groups"] == "10"
    assert params["set"] == "200" and params["page"] == "1"


def test_latest_locations_requests_small_select(api: FakeAPI, make_client) -> None:
    def provider_select(request):
        # Pegasus keeps the last projection for a repeated resource selector.
        selectors = request.url.params["select"].split(",")
        device_fields = [s.split(":", 1)[1] for s in selectors if s.startswith("device:")][-1]
        rows = [vehicle_payload(1), vehicle_payload(2, imei=350000000000002)]
        if "latest.loc" not in device_fields.split():
            for row in rows:
                row["device"].pop("latest")
        return page(rows)

    api.add("GET", VEHICLES, provider_select)
    result = make_client().latest_locations([1, 2, 2])
    assert all(
        v.latest_location is not None and v.latest_location.fix.is_usable for v in result.items
    )
    assert all(v.connection is not None for v in result.items)
    assert [v.id for v in result.items] == [1, 2]
    params = api.requests[0].url.params
    assert params["ids"] == "1,2"
    assert params["select"] == "id,name,device:latest.loc connection"


def test_pagination_walks_all_pages(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        VEHICLES,
        page([vehicle_payload(1)], page_no=1, pages=2, total=2),
        page([vehicle_payload(2)], page_no=2, pages=2, total=2),
    )
    result = make_client().list_vehicles(page_size=1)
    assert [v.id for v in result.items] == [1, 2]
    assert result.total == 2 and result.pages_read == 2 and not result.truncated


def test_pagination_is_bounded_and_reports_truncation(api: FakeAPI, make_client) -> None:
    def endless(request):
        n = int(request.url.params["page"])
        return page([vehicle_payload(n)], page_no=n, pages=999, total=999)

    api.add("GET", VEHICLES, endless)
    result = make_client(max_pages=3).list_vehicles(page_size=1)
    assert result.truncated is True
    assert result.pages_read == 3
    assert len(api.requests) == 3


def test_empty_collection(api: FakeAPI, make_client) -> None:
    api.add(
        "GET",
        VEHICLES,
        json_response(200, {"set": 0, "total": 0, "page": 1, "pages": 0, "data": []}),
    )
    result = make_client().list_vehicles()
    assert result.items == () and result.total == 0 and not result.truncated


def test_malformed_collection_raises(api: FakeAPI, make_client) -> None:
    api.add("GET", VEHICLES, json_response(200, {"unexpected": True}))
    with pytest.raises(ResponseFormatError):
        make_client().list_vehicles()


def test_vehicle_without_id_raises() -> None:
    with pytest.raises(ResponseFormatError):
        Vehicle.from_api({"name": "no id"})


def test_vehicle_with_scalar_device_and_no_location() -> None:
    vehicle = Vehicle.from_api({"id": 3, "device": 350000000000009, "groups": ["x", 4]})
    assert vehicle.device_imei == 350000000000009
    assert vehicle.latest_location is None
    assert vehicle.groups == (4,)


def test_get_vehicle_and_device(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/vehicles/9", json_response(200, vehicle_payload(9)))
    api.add(
        "GET",
        "/v2/api/devices/350000000000001",
        json_response(
            200,
            {
                "imei": 350000000000001,
                "vehicle": {"id": 9, "name": "Unit 9"},
                "connection": {"online": False, "_epoch": 1_760_000_100},
                "lastrx": {"value": 1_760_000_050.1},
                "version": {"number": "1.2.3", "vkey_model": "ExampleTracker"},
            },
        ),
    )
    client = make_client()
    assert client.get_vehicle(9).id == 9
    device = client.get_device(350000000000001)
    assert device.vehicle_id == 9
    assert device.connection is not None and device.connection.online is False
    assert device.firmware_version == "1.2.3" and device.model == "ExampleTracker"
    with pytest.raises(ValueError):
        client.get_vehicle(-1)


def test_device_identifier_differences() -> None:
    # IMEIs may arrive as numbers or numeric strings; vehicle ids are separate ids.
    a = Device.from_api({"imei": "350000000000001", "vehicle": {"id": "9"}})
    b = Device.from_api({"imei": 350000000000001, "vehicle": {"id": 9}})
    assert a.imei == b.imei == 350000000000001
    assert a.vehicle_id == b.vehicle_id == 9
    with pytest.raises(ResponseFormatError):
        Device.from_api({"imei": "not-an-imei"})


def test_list_devices_always_selects_imei(api: FakeAPI, make_client) -> None:
    api.add("GET", "/v2/api/devices", json_response(200, {"data": [{"imei": 1}], "pages": 1}))
    make_client().list_devices(imeis=[1], select=["latest.loc"])
    assert api.requests[0].url.params["select"] == "imei,latest.loc"


# -- GPS fix semantics -----------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "valid", "issue"),
    [
        (None, -66.0, True, PositionIssue.MISSING),
        ("abc", -66.0, True, PositionIssue.NOT_NUMERIC),
        (91.0, -66.0, True, PositionIssue.OUT_OF_RANGE),
        (18.0, -181.0, True, PositionIssue.OUT_OF_RANGE),
        (0.0, 0.0, True, PositionIssue.NULL_ISLAND),
        (18.0, -66.0, False, PositionIssue.DEVICE_FLAGGED_INVALID),
        (float("nan"), -66.0, True, PositionIssue.NOT_NUMERIC),
        (True, -66.0, True, PositionIssue.NOT_NUMERIC),
    ],
)
def test_invalid_positions_are_flagged(lat, lon, valid, issue) -> None:
    fix = GpsFix.build(lat, lon, device_valid=valid)
    assert fix.issue is issue
    assert not fix.is_usable


def test_valid_flag_unknown_is_not_false() -> None:
    fix = GpsFix.build(18.0, -66.0, device_valid=None)
    assert fix.device_valid is None
    assert fix.is_usable


def test_tri_state_bool() -> None:
    assert to_bool(True) is True and to_bool("false") is False
    assert to_bool(None) is None and to_bool("maybe") is None and to_bool(2) is None


# -- timestamps ------------------------------------------------------------------


def test_timestamp_normalization() -> None:
    expected = datetime(2025, 10, 9, 12, 0, 0, tzinfo=UTC)
    assert parse_timestamp(int(expected.timestamp())) == expected
    assert parse_timestamp(expected.timestamp() * 1000) == expected
    assert parse_timestamp("2025-10-09T12:00:00") == expected
    assert parse_timestamp("2025-10-09 12:00:00") == expected
    assert parse_timestamp("2025-10-09T08:00:00-04:00") == expected
    assert parse_timestamp("2025-10-09T12:00:00Z") == expected
    assert parse_timestamp(str(int(expected.timestamp()))) == expected


@pytest.mark.parametrize("value", [None, "", "garbage", 0, -5, 1e20, True, float("inf")])
def test_implausible_timestamps_are_none(value) -> None:
    assert parse_timestamp(value) is None


def test_api_datetime_requires_aware_values() -> None:
    assert format_api_datetime(datetime(2025, 1, 1, 4, tzinfo=UTC)) == "2025-01-01T04:00:00"
    with pytest.raises(ValueError):
        format_api_datetime(datetime(2025, 1, 1))


def test_speed_conversions() -> None:
    assert Speed(60, SpeedUnit.MPH).kph == pytest.approx(96.56064)
    assert Speed(100, SpeedUnit.KPH).mph == pytest.approx(62.137119)
    assert Speed(10, SpeedUnit.MPS).kph == pytest.approx(36.0)


# -- parameter safety ------------------------------------------------------------


def test_param_builders_reject_injection() -> None:
    with pytest.raises(ValueError):
        field_list(["lat&auth=x"])
    with pytest.raises(ValueError):
        search_params({"auth": "x"})
    with pytest.raises(ValueError):
        search_params({"name&x": "y"})
    with pytest.raises(ValueError):
        id_list([1, True])
    with pytest.raises(ValueError):
        id_list([])
    with pytest.raises(ValueError):
        id_list(range(1, 1000))
    assert search_params({"info.make": "Honda"}) == {"search.info.make": "Honda"}


def test_time_window_bounds() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        time_window(start, start, max_window=timedelta(days=1))
    with pytest.raises(ValueError):
        time_window(start, start + timedelta(days=2), max_window=timedelta(days=1))
    with pytest.raises(ValueError):
        time_window(datetime(2025, 1, 1), datetime(2025, 1, 2), max_window=timedelta(days=2))


# -- redaction & URLs -------------------------------------------------------------


def test_redaction() -> None:
    secret = Secret("s3cr3t-value")
    assert "s3cr3t" not in repr(secret) and "s3cr3t" not in f"{secret}"
    text = redact_text('{"auth": "abc123", "password": "pw"} ?auth=zzz s3cr3t-value', secret)
    assert "abc123" not in text and 'pw"' not in text and "zzz" not in text
    assert "s3cr3t-value" not in text
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(secret)


def test_live_url_validation() -> None:
    assert (
        validate_live_url("https://aws-live-9.pegasusgateway.com")
        == "https://aws-live-9.pegasusgateway.com"
    )
    assert validate_live_url("https://x.peginstances.com/") == "https://x.peginstances.com"
    for bad in (
        None,
        "",
        "http://aws-live-9.pegasusgateway.com",
        "https://pegasusgateway.com.evil.example",
        "https://evilpegasusgateway.com",
        "https://u:p@aws-live-9.pegasusgateway.com",
        "https://aws-live-9.pegasusgateway.com:8443",
        "https://aws-live-9.pegasusgateway.com/socket?x=1",
    ):
        with pytest.raises(UnsafeURLError):
            validate_live_url(bad)
    assert validate_live_url("https://live.example.test", allowed_host_suffixes=("example.test",))


@pytest.mark.parametrize(
    "selector", ["device:latest.loc connection", "device:latest.vcounters+latest.counters"]
)
def test_nested_resource_field_selection(selector):
    assert field_list(["id", selector]) == "id," + selector


@pytest.mark.parametrize(
    "selector",
    [
        "lat lon",
        "device:latest.loc&auth=x",
        "device:latest.loc\n",
        "device:latest.loc  connection",
        "device:latest.loc+",
        "device:latest.loc\tconnection",
    ],
)
def test_nested_resource_field_selection_rejects_invalid_syntax(selector):
    with pytest.raises(ValueError):
        field_list([selector])
