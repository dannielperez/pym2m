"""Read-only REST client."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx

from pym2m.auth import Credentials, TokenStore
from pym2m.exceptions import ResponseFormatError, UnsupportedResponseError
from pym2m.models import (
    Alert,
    ApiInfo,
    CounterTotals,
    Device,
    EventSource,
    Geofence,
    Group,
    PagedResult,
    ReverseGeocode,
    TelemetryEvent,
    Trigger,
    Trip,
    Vehicle,
)
from pym2m.normalize import PositionIssue, format_api_datetime, validate_coordinates
from pym2m.params import field_list, id_list, search_params, time_window
from pym2m.transport import DEFAULT_TIMEOUT, HTTPTransport, RetryPolicy

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000
MAX_DEVICE_PAGE_SIZE = 500
MAX_HISTORY_ENTITIES = 50
LATEST_LOCATION_SELECT = ("id", "name", "device:latest.loc connection")
# Documented master fields needed for a normalized TelemetryEvent.
RAWDATA_FIELDS = (
    "id",
    "vid",
    "device_id",
    "event_time",
    "system_time",
    "lat",
    "lon",
    "mph",
    "head",
    "al",
    "sv",
    "hdop",
    "pdop",
    "valid_position",
    "io_ign",
    "moving",
    "code",
    "label",
    "trip_id",
)
# Explicit units make results unambiguous and server-cache friendly.
TRIP_UNITS = {"distance": "meter", "time": "second", "speed": "kph"}
COUNTER_UNITS = {"distance": "meter", "time": "second", "volume": "liter"}


class M2MClient:
    """Read-only client for a Pegasus-Gateway-compatible tracking API.

    >>> from pym2m import M2MClient, TokenCredentials, Secret
    >>> client = M2MClient(  # doctest: +SKIP
    ...     "https://tracking.example.com/api/",
    ...     TokenCredentials(Secret("<read-only application token>")),
    ... )

    The client exposes no method that changes platform state. It is safe to share
    across threads; the token lifecycle is lock-protected.
    """

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        verify: bool | str = True,
        retry: RetryPolicy | None = None,
        token_idle_ttl_s: float = 55 * 60,
        max_pages: int = 20,
        max_history_window: timedelta = timedelta(hours=24),
        http_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        self.max_pages = max_pages
        if not timedelta(minutes=1) <= max_history_window <= timedelta(days=31):
            raise ValueError("max_history_window must be between 1 minute and 31 days")
        self.max_history_window = max_history_window
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self._transport = HTTPTransport(
            base_url,
            TokenStore(credentials, idle_ttl_s=token_idle_ttl_s),
            timeout=timeout,
            verify=verify,
            retry=retry,
            http_transport=http_transport,
            **kwargs,
        )

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    def raw_get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """Escape hatch for documented read endpoints pym2m does not wrap yet."""
        return self._transport.get(path, params)

    # -- platform ----------------------------------------------------------------

    def api_info(self) -> ApiInfo:
        """The API root: version and the advertised realtime ``live_url``."""
        return ApiInfo.from_api(self._transport.get("", None))

    # -- vehicles ----------------------------------------------------------------

    def list_vehicles(
        self,
        *,
        groups: Iterable[int] | None = None,
        ids: Iterable[int] | None = None,
        search: dict[str, str | int] | None = None,
        select: Iterable[str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> PagedResult[Vehicle]:
        """List vehicles visible to the token, walking at most ``max_pages`` pages."""
        params: dict[str, str] = {}
        if groups is not None:
            params["groups"] = id_list(groups, name="groups")
        if ids is not None:
            params["ids"] = id_list(ids, name="ids")
        if select is not None:
            params["select"] = field_list(select, name="select")
        params.update(search_params(search))
        return self._paginate("vehicles", params, Vehicle.from_api, page_size, MAX_PAGE_SIZE)

    def get_vehicle(self, vehicle_id: int) -> Vehicle:
        id_list([vehicle_id], name="vehicle_id")
        return Vehicle.from_api(self._transport.get(f"vehicles/{vehicle_id}", None))

    def latest_locations(
        self,
        vehicle_ids: Iterable[int] | None = None,
        *,
        groups: Iterable[int] | None = None,
    ) -> PagedResult[Vehicle]:
        """Vehicles with only their identity, latest location and connection state.

        Uses ``select`` so the platform returns a small payload. Pass explicit
        ``vehicle_ids`` or ``groups``; with neither, every visible vehicle is read.
        """
        return self.list_vehicles(ids=vehicle_ids, groups=groups, select=LATEST_LOCATION_SELECT)

    # -- devices -----------------------------------------------------------------

    def list_devices(
        self,
        *,
        imeis: Iterable[int] | None = None,
        select: Iterable[str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> PagedResult[Device]:
        params: dict[str, str] = {}
        if imeis is not None:
            params["imeis"] = id_list(imeis, name="imeis")
        if select is not None:
            selected = list(select)
            # imei is the identity; always request it so results can be parsed.
            params["select"] = field_list(["imei", *[s for s in selected if s != "imei"]])
        return self._paginate("devices", params, Device.from_api, page_size, MAX_DEVICE_PAGE_SIZE)

    def get_device(self, imei: int) -> Device:
        id_list([imei], name="imei")
        return Device.from_api(self._transport.get(f"devices/{imei}", None))

    # -- history -----------------------------------------------------------------

    def rawdata(
        self,
        entities: Iterable[int],
        start: datetime,
        end: datetime,
        *,
        fields: Iterable[str] = RAWDATA_FIELDS,
        source: EventSource = EventSource.REST_RAWDATA,
    ) -> list[TelemetryEvent]:
        """Historical events for vehicle/asset ids in ``[start, end]``, oldest first.

        The window is capped at ``max_history_window`` and the entity count at
        ``MAX_HISTORY_ENTITIES``: rawdata is rate limited per user and globally.
        """
        params = self._history_params(entities, start, end, name="entities")
        params["fields"] = field_list(fields)
        body = self._transport.get("rawdata", params)
        if not isinstance(body, Mapping) or not isinstance(body.get("events"), list):
            raise UnsupportedResponseError(
                "rawdata did not return an 'events' list (large queries can be answered "
                "with an asynchronous job, which pym2m does not follow); narrow the window"
            )
        events = [TelemetryEvent.from_fields(e, source=source) for e in body["events"]]
        epoch = datetime.min.replace(tzinfo=UTC)
        return sorted(events, key=lambda e: (e.event_time or epoch, e.event_id or 0))

    def trips(
        self,
        vehicles: Iterable[int],
        start: datetime,
        end: datetime,
        *,
        fields: Iterable[str] | None = None,
    ) -> list[Trip]:
        """Trips for vehicles in the window, in meters / seconds / km/h."""
        params = self._history_params(vehicles, start, end, name="vehicles")
        params.update(TRIP_UNITS)
        if fields is not None:
            params["fields"] = field_list(fields)
        body = self._transport.get("trips", params)
        units = {"distance": "meter", "time": "second", "speed": "kph"}
        if isinstance(body, list):
            return [Trip.from_api(item, units=units) for item in body]
        if isinstance(body, Mapping) and not isinstance(body.get("data"), list):
            # A single summary object is returned for some field combinations.
            return [Trip.from_api(body, units=units)]
        if isinstance(body, Mapping):
            return [Trip.from_api(item, units=units) for item in body["data"]]
        raise ResponseFormatError("trips returned an unexpected body")

    def counters(
        self, vehicles: Iterable[int], start: datetime, end: datetime
    ) -> list[CounterTotals]:
        """Counter differences (distance, ignition time, ...) per vehicle in the window."""
        params = self._history_params(vehicles, start, end, name="vehicles")
        params.update(COUNTER_UNITS)
        body = self._transport.get("counters", params)
        if not isinstance(body, Mapping) or not isinstance(body.get("counters"), list):
            raise ResponseFormatError("counters did not return a 'counters' list")
        reported = body.get("units")
        units = (
            {str(k): str(v) for k, v in reported.items()} if isinstance(reported, Mapping) else {}
        )
        return [CounterTotals.from_api(c, units=units) for c in body["counters"]]

    def vehicle_counters(self, vehicle_id: int) -> dict[str, Any]:
        """Current counters of one vehicle, as returned (shape not normalized)."""
        id_list([vehicle_id], name="vehicle_id")
        body = self._transport.get(f"vehicles/{vehicle_id}/counters", None)
        if not isinstance(body, Mapping):
            raise ResponseFormatError("vehicle counters returned a non-object body")
        return dict(body)

    # -- reference data ----------------------------------------------------------

    def list_geofences(
        self,
        *,
        groups: Iterable[int] | None = None,
        search: dict[str, str | int] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> PagedResult[Geofence]:
        params: dict[str, str] = {}
        if groups is not None:
            params["groups"] = id_list(groups, name="groups")
        params.update(search_params(search))
        return self._paginate("geofences", params, Geofence.from_api, page_size, MAX_PAGE_SIZE)

    def list_groups(self, *, page_size: int = DEFAULT_PAGE_SIZE) -> PagedResult[Group]:
        """Groups visible to the token (a scoped token sees only its groups)."""
        return self._paginate("groups", {}, Group.from_api, page_size, MAX_PAGE_SIZE)

    def list_triggers(self, *, page_size: int = DEFAULT_PAGE_SIZE) -> PagedResult[Trigger]:
        return self._paginate("triggers", {}, Trigger.from_api, page_size, MAX_PAGE_SIZE)

    def alerts(self, start: datetime, *, triggers: Iterable[int] | None = None) -> list[Alert]:
        """Alerts fired since ``start`` (bounded by ``max_history_window``)."""
        now = datetime.now(UTC)
        time_window(
            start, max(now, start + timedelta(seconds=1)), max_window=self.max_history_window
        )
        params = {"from": format_api_datetime(start)}
        if triggers is not None:
            params["triggers"] = id_list(triggers, name="triggers")
        body = self._transport.get("alerts", params)
        if isinstance(body, Mapping) and isinstance(body.get("data"), list):
            body = body["data"]
        if not isinstance(body, list):
            raise ResponseFormatError("alerts returned an unexpected body")
        return [Alert.from_api(a) for a in body]

    def reverse_geocode(self, lat: float, lon: float) -> ReverseGeocode:
        checked = validate_coordinates(lat, lon)
        if isinstance(checked, PositionIssue):
            raise ValueError(f"invalid coordinates: {checked.value}")
        params = {"lat": f"{checked[0]:.6f}", "lon": f"{checked[1]:.6f}"}
        return ReverseGeocode.from_api(self._transport.get("reversegeo", params))

    def _history_params(
        self, ids: Iterable[int], start: datetime, end: datetime, *, name: str
    ) -> dict[str, str]:
        id_values = list(ids)
        if len(set(id_values)) > MAX_HISTORY_ENTITIES:
            raise ValueError(f"{name} accepts at most {MAX_HISTORY_ENTITIES} ids per query")
        params = time_window(start, end, max_window=self.max_history_window)
        params[name] = id_list(id_values, name=name)
        return params

    # -- pagination --------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        params: dict[str, str],
        parse: Callable[[Any], T],
        page_size: int,
        max_page_size: int,
    ) -> PagedResult[T]:
        if not 1 <= page_size <= max_page_size:
            raise ValueError(f"page_size must be between 1 and {max_page_size}")
        items: list[T] = []
        page, pages_read = 1, 0
        total: int | None = None
        while True:
            body = self._transport.get(path, {**params, "set": str(page_size), "page": str(page)})
            pages_read += 1
            if isinstance(body, list):
                # Some collections are returned unpaginated.
                items.extend(parse(entry) for entry in body)
                return PagedResult(tuple(items), len(items), pages_read, False)
            if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
                raise ResponseFormatError(f"{path}: expected a paginated object with 'data'")
            items.extend(parse(entry) for entry in body["data"])
            reported_total, reported_pages = body.get("total"), body.get("pages")
            if isinstance(reported_total, int):
                total = reported_total
            last_page = reported_pages if isinstance(reported_pages, int) else page
            if page >= last_page or not body["data"]:
                return PagedResult(tuple(items), total, pages_read, False)
            if pages_read >= self.max_pages:
                return PagedResult(tuple(items), total, pages_read, True)
            page += 1
