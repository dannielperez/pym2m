"""Parser for forwarder (HTTP push) payloads.

A forwarder posts a JSON array of events to a receiver you operate. The receiver
acknowledges with a JSON array of the event ids it failed to process (``[]`` means
all accepted), according to public Pegasus documentation. Confirm this contract
with the actual operator before activation; this SDK does not verify durability.

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
from pym2m.normalize import to_int

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
    duplicates: int = 0

    def ack_body(self, failed_event_ids: list[int] | None = None) -> list[int]:
        """Response body for the platform: ids to retry.

        Rejected items are malformed and would fail again, so they are not
        re-queued; pass ids that failed downstream processing in
        ``failed_event_ids`` to ask for redelivery.
        """
        known = {event.event_id for event in self.events}
        failed = failed_event_ids or []
        if any(type(eid) is not int or eid not in known for eid in failed):
            raise ValueError("retry ids must identify parsed events in this batch")
        return sorted(set(failed))


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
    if type(max_body_bytes) is not int or max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be a positive integer")
    if type(max_events) is not int or max_events <= 0:
        raise ValueError("max_events must be a positive integer")
    if isinstance(body, bytes | str):
        size = len(body.encode("utf-8", errors="surrogatepass") if isinstance(body, str) else body)
        if size > max_body_bytes:
            raise ResponseFormatError(f"forwarder body exceeds {max_body_bytes} bytes")
        try:
            decoded = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ResponseFormatError("forwarder body is not valid JSON") from None
    else:
        decoded = body
    if isinstance(decoded, Mapping):
        decoded = [decoded]
    if not isinstance(decoded, list):
        raise ResponseFormatError("forwarder body must be a JSON array of events")
    if len(decoded) > max_events:
        raise ResponseFormatError(f"forwarder batch exceeds {max_events} events")

    # Bound predecoded inputs too. Iterative validation rejects excessive nesting,
    # non-JSON values and non-finite numbers without copying their raw contents.
    stack = [(decoded, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > max_body_bytes:
            raise ResponseFormatError("forwarder body is too deeply nested or complex")
        if isinstance(value, str) and len(value) > max_body_bytes:
            raise ResponseFormatError(f"forwarder body exceeds {max_body_bytes} bytes")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ResponseFormatError("forwarder object keys must be strings")
            if any(len(key) > max_body_bytes for key in value):
                raise ResponseFormatError("forwarder object key exceeds size limit")
            if len(value) + nodes + len(stack) > max_body_bytes:
                raise ResponseFormatError("forwarder body is too complex")
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            if len(value) + nodes + len(stack) > max_body_bytes:
                raise ResponseFormatError("forwarder body is too complex")
            stack.extend((child, depth + 1) for child in value)
    size = 0
    try:
        for chunk in json.JSONEncoder(allow_nan=False).iterencode(decoded):
            size += len(chunk.encode())
            if size > max_body_bytes:
                raise ResponseFormatError(f"forwarder body exceeds {max_body_bytes} bytes")
    except (TypeError, ValueError, RecursionError):
        raise ResponseFormatError("forwarder body contains invalid JSON values") from None

    events: list[TelemetryEvent] = []
    rejected: list[RejectedItem] = []
    seen: dict[str, TelemetryEvent] = {}
    duplicates = 0
    for index, item in enumerate(decoded):
        fields = item.get("event", item) if isinstance(item, Mapping) else None
        raw_id = fields.get("id") if isinstance(fields, Mapping) else None
        try:
            event_id = to_int(raw_id)
        except (ValueError, OverflowError):
            event_id = None
        if event_id is None or event_id <= 0:
            rejected.append(RejectedItem(index, None, "missing or invalid event id"))
            continue
        try:
            if isinstance(item, Mapping) and isinstance(item.get("event"), Mapping):
                event = TelemetryEvent.from_vehicle_event(item, source=EventSource.FORWARDER)
            else:
                event = TelemetryEvent.from_fields(item, source=EventSource.FORWARDER)
        except (ResponseFormatError, ValueError, OverflowError):
            rejected.append(RejectedItem(index, event_id, "invalid telemetry fields"))
            continue
        if event.vehicle_id is None and event.device_imei is None:
            rejected.append(RejectedItem(index, event.event_id, "no vehicle id or device id"))
            continue
        if event.event_time is None:
            rejected.append(RejectedItem(index, event.event_id, "missing or invalid event_time"))
            continue
        if event.dedup_key in seen:
            previous = seen[event.dedup_key]
            if previous.vehicle_id != event.vehicle_id or previous.raw != event.raw:
                raise ResponseFormatError("conflicting duplicate event id")
            duplicates += 1
            continue
        seen[event.dedup_key] = event
        events.append(event)
    return ForwarderBatch(tuple(events), tuple(rejected), duplicates)
