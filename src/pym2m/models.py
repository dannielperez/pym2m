"""Typed data contracts.

Each model keeps the provider payload it was built from in ``raw`` so callers can
reach fields pym2m does not interpret. Only fields whose meaning is publicly
documented are promoted to attributes; everything else stays in ``raw`` without
implied semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pym2m.exceptions import ResponseFormatError
from pym2m.normalize import (
    PositionIssue,
    Speed,
    SpeedUnit,
    parse_timestamp,
    to_bool,
    to_float,
    to_int,
    validate_coordinates,
)

JSON = dict[str, Any]
T = TypeVar("T")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_list(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(i for i in (to_int(v) for v in value) if i is not None)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


@dataclass(frozen=True, slots=True)
class GpsFix:
    """A reported position and its quality metadata.

    ``lat``/``lon`` are set only when they are numeric and in range. ``issue``
    explains why a fix is unusable. ``device_valid`` is the tracker's own GPS-valid
    flag. It says the receiver had a fix, not that the fix is precise enough to be
    evidence of presence at a specific point.

    ``accuracy_m`` is always ``None``: the documented payloads carry no accuracy in
    metres. DOP values (``hdop``/``pdop``) are unitless quality hints and are not
    converted into accuracy.
    """

    lat: float | None
    lon: float | None
    issue: PositionIssue | None
    device_valid: bool | None = None
    heading_deg: float | None = None
    altitude_m: float | None = None
    satellites: int | None = None
    hdop: float | None = None
    pdop: float | None = None
    accuracy_m: float | None = None

    @property
    def is_usable(self) -> bool:
        return self.issue is None and self.lat is not None and self.lon is not None

    @classmethod
    def build(
        cls,
        lat: Any,
        lon: Any,
        *,
        device_valid: Any = None,
        heading: Any = None,
        altitude: Any = None,
        satellites: Any = None,
        hdop: Any = None,
        pdop: Any = None,
    ) -> GpsFix:
        valid_flag = to_bool(device_valid)
        checked = validate_coordinates(lat, lon)
        issue: PositionIssue | None
        if isinstance(checked, PositionIssue):
            flat = flon = None
            issue = checked
        else:
            flat, flon = checked
            issue = PositionIssue.DEVICE_FLAGGED_INVALID if valid_flag is False else None
        return cls(
            lat=flat,
            lon=flon,
            issue=issue,
            device_valid=valid_flag,
            heading_deg=to_float(heading),
            altitude_m=to_float(altitude),
            satellites=to_int(satellites),
            hdop=to_float(hdop),
            pdop=to_float(pdop),
        )


@dataclass(frozen=True, slots=True)
class VehicleInfo:
    make: str | None = None
    model: str | None = None
    year: int | None = None
    vin: str | None = None
    license_plate: str | None = None
    alias: str | None = None
    color: str | None = None
    description: str | None = None

    @classmethod
    def from_api(cls, data: Any) -> VehicleInfo:
        info = _mapping(data)
        return cls(
            make=_str(info.get("make")),
            model=_str(info.get("model")),
            year=to_int(info.get("year")),
            vin=_str(info.get("vin")),
            license_plate=_str(info.get("license_plate")),
            alias=_str(info.get("alias")),
            color=_str(info.get("color")),
            description=_str(info.get("description")),
        )


@dataclass(frozen=True, slots=True)
class LatestLocation:
    """The most recent location a device reported (``device.latest.loc``)."""

    fix: GpsFix
    event_time: datetime | None
    received_time: datetime | None
    speed: Speed | None
    moving: bool | None
    event_code: int | None
    event_label: str | None
    trip_id: int | None
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> LatestLocation | None:
        loc = _mapping(data)
        if not loc:
            return None
        mph = to_float(loc.get("mph"))
        return cls(
            fix=GpsFix.build(
                loc.get("lat"),
                loc.get("lon"),
                device_valid=loc.get("valid"),
                heading=loc.get("head"),
                altitude=loc.get("al"),
                satellites=loc.get("sv"),
                hdop=loc.get("hdop"),
                pdop=loc.get("pdop"),
            ),
            event_time=parse_timestamp(loc.get("evtime")),
            received_time=parse_timestamp(loc.get("systime")),
            speed=Speed(mph, SpeedUnit.MPH) if mph is not None else None,
            moving=to_bool(loc.get("moving")),
            event_code=to_int(loc.get("code")),
            event_label=_str(loc.get("label")),
            trip_id=to_int(loc.get("trip_id")),
            raw=dict(loc),
        )


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """Whether the tracking device currently holds a connection to the platform."""

    online: bool | None
    changed_at: datetime | None

    @classmethod
    def from_api(cls, data: Any) -> ConnectionState | None:
        conn = _mapping(data)
        if not conn:
            return None
        return cls(
            online=to_bool(conn.get("online")), changed_at=parse_timestamp(conn.get("_epoch"))
        )


@dataclass(frozen=True, slots=True)
class Vehicle:
    """A tracked vehicle entity. ``device_imei`` is the currently linked tracker."""

    id: int
    name: str | None
    device_imei: int | None
    groups: tuple[int, ...]
    info: VehicleInfo
    latest_location: LatestLocation | None
    connection: ConnectionState | None
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> Vehicle:
        item = _mapping(data)
        vid = to_int(item.get("id"))
        if vid is None:
            raise ResponseFormatError("vehicle payload has no integer 'id'")
        device = item.get("device")
        device_map = _mapping(device)
        imei = to_int(device_map.get("imei")) if device_map else to_int(device)
        return cls(
            id=vid,
            name=_str(item.get("name")),
            device_imei=imei,
            groups=_int_list(item.get("groups")),
            info=VehicleInfo.from_api(item.get("info")),
            latest_location=LatestLocation.from_api(_mapping(device_map.get("latest")).get("loc")),
            connection=ConnectionState.from_api(device_map.get("connection")),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """A tracking device, identified by IMEI."""

    imei: int
    vehicle_id: int | None
    connection: ConnectionState | None
    latest_location: LatestLocation | None
    last_received_at: datetime | None
    firmware_version: str | None
    model: str | None
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> Device:
        item = _mapping(data)
        imei = to_int(item.get("imei"))
        if imei is None:
            raise ResponseFormatError("device payload has no integer 'imei'")
        version = _mapping(item.get("version"))
        return cls(
            imei=imei,
            vehicle_id=to_int(_mapping(item.get("vehicle")).get("id")),
            connection=ConnectionState.from_api(item.get("connection")),
            latest_location=LatestLocation.from_api(_mapping(item.get("latest")).get("loc")),
            last_received_at=parse_timestamp(_mapping(item.get("lastrx")).get("value")),
            firmware_version=_str(version.get("number")),
            model=_str(version.get("vkey_model")) or _str(version.get("device")),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class ApiInfo:
    """The API root document. ``live_url`` is unvalidated here; see ``urls.validate_live_url``."""

    version: str | None
    live_url: str | None
    site_id: int | None
    domain: str | None
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> ApiInfo:
        root = _mapping(data)
        if not root:
            raise ResponseFormatError("API root returned an empty or non-object body")
        return cls(
            version=_str(root.get("tag")) or _str(root.get("name")),
            live_url=_str(root.get("live_url")),
            site_id=to_int(root.get("pegasus_id")),
            domain=_str(root.get("domain")),
            raw=dict(root),
        )


@dataclass(frozen=True, slots=True)
class PagedResult(Generic[T]):
    """A bounded walk over a paginated collection.

    ``truncated`` is ``True`` when the walk stopped at ``max_pages`` before the
    platform's last page; callers must not treat a truncated result as complete.
    """

    items: tuple[T, ...]
    total: int | None
    pages_read: int
    truncated: bool


# -- history, live and forwarder telemetry ------------------------------------------


class EventSource(StrEnum):
    """Where a telemetry event came from. Kept for provenance and deduplication."""

    REST_RAWDATA = "rest_rawdata"
    REST_RECOVERY = "rest_recovery"
    LIVE = "live"
    FORWARDER = "forwarder"


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """One device event, normalized from rawdata, a live event or a forwarder post.

    ``event_id`` is the platform's unique event id when present. ``dedup_key`` is
    stable across REST, live and forwarder delivery of the same event.
    ``device_online`` is only known for live/forwarder payloads that carry device
    state; REST history leaves it ``None``.
    """

    event_id: int | None
    vehicle_id: int | None
    device_imei: int | None
    event_time: datetime | None
    received_time: datetime | None
    fix: GpsFix
    speed: Speed | None
    ignition: bool | None
    moving: bool | None
    device_online: bool | None
    event_code: int | None
    event_label: str | None
    trip_id: int | None
    source: EventSource
    raw: JSON = field(repr=False)

    @property
    def dedup_key(self) -> str:
        if self.event_id is not None:
            return f"id:{self.event_id}"
        stamp = self.event_time.isoformat() if self.event_time else "?"
        entity = self.vehicle_id if self.vehicle_id is not None else f"imei{self.device_imei}"
        return f"v:{entity}:{stamp}:{self.event_code}:{self.fix.lat}:{self.fix.lon}"

    @classmethod
    def from_fields(
        cls,
        data: Any,
        *,
        source: EventSource,
        vehicle_id: Any = None,
        device_online: Any = None,
    ) -> TelemetryEvent:
        """Build from a flat master-fields event (rawdata / ``json_fwd``)."""
        event = _mapping(data)
        if not event:
            raise ResponseFormatError("telemetry event is not an object")
        mph = to_float(event.get("mph"))
        return cls(
            event_id=to_int(event.get("id")),
            vehicle_id=to_int(_first(event, "vid")) if vehicle_id is None else to_int(vehicle_id),
            device_imei=to_int(event.get("device_id")),
            event_time=parse_timestamp(
                _first(event, "event_time", "event_time_epoch", "event_epoch")
            ),
            received_time=parse_timestamp(
                _first(event, "system_time", "system_time_epoch", "system_epoch")
            ),
            fix=GpsFix.build(
                event.get("lat"),
                event.get("lon"),
                device_valid=event.get("valid_position"),
                heading=event.get("head"),
                altitude=event.get("al"),
                satellites=event.get("sv"),
                hdop=event.get("hdop"),
                pdop=event.get("pdop"),
            ),
            speed=Speed(mph, SpeedUnit.MPH) if mph is not None else None,
            ignition=to_bool(event.get("io_ign")),
            moving=to_bool(event.get("moving")),
            device_online=to_bool(device_online),
            event_code=to_int(event.get("code")),
            event_label=_str(event.get("label")),
            trip_id=to_int(event.get("trip_id")),
            source=source,
            raw=dict(event),
        )

    @classmethod
    def from_vehicle_event(cls, payload: Any, *, source: EventSource) -> TelemetryEvent:
        """Build from a ``vehicle-events`` payload: ``{event, device, primary_id, ...}``."""
        body = _mapping(payload)
        event = _mapping(body.get("event"))
        if not event:
            raise ResponseFormatError("vehicle-events payload has no 'event' object")
        connection = _mapping(_mapping(body.get("device")).get("connection"))
        built = cls.from_fields(
            event,
            source=source,
            vehicle_id=_first(event, "vid")
            if body.get("primary_id") is None
            else body["primary_id"],
            device_online=connection.get("online"),
        )
        return built


@dataclass(frozen=True, slots=True)
class Trip:
    """A trip (segment) summary.

    Values are in the units pym2m requested (``units``). Which fields are present
    depends on the ``fields`` requested; absent fields are ``None``.
    """

    id: int | None
    vehicle_id: int | None
    start_time: datetime | None
    end_time: datetime | None
    distance: float | None
    duration: float | None
    mean_speed: float | None
    max_speed: float | None
    moving: bool | None
    units: Mapping[str, str]
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any, *, units: Mapping[str, str]) -> Trip:
        item = _mapping(data)
        if not item:
            raise ResponseFormatError("trip entry is not an object")
        return cls(
            id=to_int(item.get("id")),
            vehicle_id=to_int(_first(item, "vid", "src_id")),
            start_time=parse_timestamp(_first(item, "start_time", "start_time__epoch")),
            end_time=parse_timestamp(_first(item, "end_time", "end_time__epoch")),
            distance=to_float(item.get("distance")),
            duration=to_float(item.get("duration")),
            mean_speed=to_float(item.get("mean_speed")),
            max_speed=to_float(item.get("max_speed")),
            moving=to_bool(item.get("moving")),
            units=dict(units),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class CounterTotals:
    """Counter differences for one vehicle over a window (``/counters``).

    ``values`` holds every numeric counter returned, such as ``dev_dist`` (device
    odometer distance) and ``dev_ign`` (ignition time), in ``units``.
    """

    vehicle_id: int | None
    start: datetime | None
    end: datetime | None
    values: Mapping[str, float]
    units: Mapping[str, str]
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any, *, units: Mapping[str, str]) -> CounterTotals:
        item = _mapping(data)
        values = {
            key: number
            for key, value in item.items()
            if key not in ("vid", "from", "to") and (number := to_float(value)) is not None
        }
        return cls(
            vehicle_id=to_int(item.get("vid")),
            start=parse_timestamp(item.get("from")),
            end=parse_timestamp(item.get("to")),
            values=values,
            units=dict(units),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class Geofence:
    """A platform geofence. Geometry stays in ``raw``; its encoding varies by type."""

    id: int
    name: str | None
    groups: tuple[int, ...]
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> Geofence:
        item = _mapping(data)
        gid = to_int(item.get("id"))
        if gid is None:
            raise ResponseFormatError("geofence payload has no integer 'id'")
        return cls(
            id=gid,
            name=_str(item.get("name")),
            groups=_int_list(item.get("groups")),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class Trigger:
    """A configured automation rule. Rule structure stays in ``raw``."""

    id: int
    name: str | None
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> Trigger:
        item = _mapping(data)
        tid = to_int(item.get("id"))
        if tid is None:
            raise ResponseFormatError("trigger payload has no integer 'id'")
        return cls(id=tid, name=_str(item.get("name")), raw=dict(item))


@dataclass(frozen=True, slots=True)
class Alert:
    """A fired trigger instance (``/alerts``)."""

    uuid: str | None
    trigger_id: int | None
    trigger_name: str | None
    object_id: int | None
    category: str | None
    level: str | None
    resolved: bool | None
    created_at: datetime | None
    event_time: datetime | None
    fix: GpsFix
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> Alert:
        item = _mapping(data)
        if not item:
            raise ResponseFormatError("alert entry is not an object")
        return cls(
            uuid=_str(item.get("tuuid")),
            trigger_id=to_int(item.get("trigger")),
            trigger_name=_str(item.get("trigger_name")),
            object_id=to_int(item.get("object_id")),
            category=_str(item.get("category")),
            level=_str(item.get("level")),
            resolved=to_bool(item.get("resolved")),
            created_at=parse_timestamp(item.get("time")),
            event_time=parse_timestamp(item.get("fields_epoch")),
            fix=GpsFix.build(item.get("fields_lat"), item.get("fields_lon")),
            raw=dict(item),
        )


@dataclass(frozen=True, slots=True)
class ReverseGeocode:
    address: str | None
    location_full: str | None
    zones: tuple[str, ...]
    raw: JSON = field(repr=False)

    @classmethod
    def from_api(cls, data: Any) -> ReverseGeocode:
        item = _mapping(data)
        if not item:
            raise ResponseFormatError("reverse geocode returned a non-object body")
        zones = item.get("zones")
        return cls(
            address=_str(item.get("address")),
            location_full=_str(item.get("location_full")),
            zones=tuple(z for z in zones if isinstance(z, str)) if isinstance(zones, list) else (),
            raw=dict(item),
        )
