"""Read-only REST client."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx

from pym2m.auth import Credentials, TokenStore
from pym2m.exceptions import ResponseFormatError
from pym2m.models import ApiInfo, Device, PagedResult, Vehicle
from pym2m.params import field_list, id_list, search_params
from pym2m.transport import DEFAULT_TIMEOUT, HTTPTransport, RetryPolicy

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000
MAX_DEVICE_PAGE_SIZE = 500
LATEST_LOCATION_SELECT = ("id", "name", "device:latest.loc", "device:connection")


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
        http_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        self.max_pages = max_pages
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
