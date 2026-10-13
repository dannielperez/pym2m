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
