"""Parser for forwarder (HTTP push) payloads.

A forwarder posts a JSON array of events to a receiver you operate. The receiver
acknowledges with a JSON array of the event ids it failed to process (``[]`` means
all accepted), and the platform re-queues those ids.

This module only parses and validates request bodies. It does **not** provide a
web endpoint and does not authenticate senders: request authentication, source
allow-listing and replay protection are the receiver's job and must be agreed
with the platform operator before you expose an endpoint.

Accepted item shapes:

* flat master-field events (the ``json_fwd`` protocol), e.g.
  ``{"id": 1, "vid": 2, "lat": ..., "lon": ..., "mph": ..., "event_time": ...}``
* ``vehicle-events``-style objects carrying an ``event`` object, as delivered by
  the realtime socket.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pym2m.exceptions import ResponseFormatError
from pym2m.models import EventSource, TelemetryEvent

DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_EVENTS = 5000


@dataclass(frozen=True, slots=True)
class RejectedItem:
    index: int
    event_id: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class ForwarderBatch:
    events: tuple[TelemetryEvent, ...]
    rejected: tuple[RejectedItem, ...]

    def ack_body(self, failed_event_ids: list[int] | None = None) -> list[int]:
        """Response body for the platform: ids to retry.

        Rejected items are malformed and would fail again, so they are not
        re-queued; pass ids that failed downstream processing in
        ``failed_event_ids`` to ask for redelivery.
        """
        return sorted(set(failed_event_ids or []))


def parse_forwarder_body(
    body: bytes | str | list[Any],
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> ForwarderBatch:
    """Parse one forwarder POST body into normalized events.

    Raises :class:`ResponseFormatError` when the body as a whole is unusable
    (too large, not JSON, not an array, too many items). Individual malformed
    items are reported in ``rejected`` instead of failing the batch.
    """
    if isinstance(body, bytes | str):
        size = len(body.encode() if isinstance(body, str) else body)
        if size > max_body_bytes:
            raise ResponseFormatError(f"forwarder body exceeds {max_body_bytes} bytes")
        try:
            decoded = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ResponseFormatError("forwarder body is not valid JSON") from None
    else:
        decoded = body
    if isinstance(decoded, Mapping):
        decoded = [decoded]
    if not isinstance(decoded, list):
        raise ResponseFormatError("forwarder body must be a JSON array of events")
    if len(decoded) > max_events:
        raise ResponseFormatError(f"forwarder batch exceeds {max_events} events")

    events: list[TelemetryEvent] = []
    rejected: list[RejectedItem] = []
    seen: set[str] = set()
    for index, item in enumerate(decoded):
        raw_id = item.get("id") if isinstance(item, Mapping) else None
        try:
            if isinstance(item, Mapping) and isinstance(item.get("event"), Mapping):
                event = TelemetryEvent.from_vehicle_event(item, source=EventSource.FORWARDER)
            else:
                event = TelemetryEvent.from_fields(item, source=EventSource.FORWARDER)
        except ResponseFormatError as exc:
            rejected.append(
                RejectedItem(index, raw_id if isinstance(raw_id, int) else None, str(exc))
            )
            continue
        if event.vehicle_id is None and event.device_imei is None:
            rejected.append(RejectedItem(index, event.event_id, "no vehicle id or device id"))
            continue
        if event.event_time is None:
            rejected.append(RejectedItem(index, event.event_id, "missing or invalid event_time"))
            continue
        if event.dedup_key in seen:
            continue
        seen.add(event.dedup_key)
        events.append(event)
    return ForwarderBatch(tuple(events), tuple(rejected))
