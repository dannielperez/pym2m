"""Reconstruct full vehicle state from compact live updates.

With ``compact: true`` the server sends each vehicle's full payload once and then
only the keys that changed. Merge semantics follow the platform's published
reference merger, with two documented deviations:

1. **Objects merge recursively. Arrays and scalars replace.** An array in an
   update replaces the cached array wholesale and is never concatenated or merged
   index-by-index. ``null`` in an update overwrites (the key is present).
2. **A new event replaces the cached event.** When ``updates`` contains
   ``"event"``, the cached ``event`` object is replaced by the update's ``event``.
   Keys listed in ``_event_restore`` are then copied back from the previous event,
   or else from ``device.latest.data[<key>].value``.

   *Deviation:* the reference merger uses JavaScript ``||``, which treats ``0`` and
   ``false`` as missing and would drop, for example, ``io_ign: false``. pym2m
   restores a key whenever it is present in the previous event, falsy or not.

On top of reconstruction the cache provides:

* **Dedup.** The same payload may be delivered twice. Events are keyed by
  ``TelemetryEvent.dedup_key`` and a repeat is never delivered again.
* **Ordering.** An event older than the newest one seen for that vehicle is
  delivered with ``out_of_order=True`` and never replaces the cached newer event.
* **Bounds.** At most ``max_entities`` vehicles are cached (least-recently-updated
  evicted) and each vehicle remembers at most ``dedup_window`` recent event keys.
"""

from __future__ import annotations

import copy
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pym2m.exceptions import ResponseFormatError
from pym2m.models import EventSource, TelemetryEvent
from pym2m.normalize import to_bool, to_int

DEFAULT_MAX_ENTITIES = 250


def deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new dict: objects merge recursively, everything else replaces."""
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in update.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class UpdateKind(StrEnum):
    EVENT = "event"
    DEVICE = "device"


@dataclass(frozen=True, slots=True)
class LiveUpdate:
    """One change delivered to the consumer."""

    vehicle_id: int
    kind: UpdateKind
    state: dict[str, Any] = field(repr=False)
    event: TelemetryEvent | None = None
    device_online: bool | None = None
    out_of_order: bool = False


@dataclass(slots=True)
class LiveStats:
    payloads: int = 0
    delivered: int = 0
    duplicates_dropped: int = 0
    out_of_order: int = 0
    rejected: int = 0
    evicted: int = 0


@dataclass(slots=True)
class _VehicleTrack:
    state: dict[str, Any]
    newest_event_time: datetime | None = None
    last_received_monotonic: float | None = None
    recent_keys: deque[str] = field(default_factory=deque)
    recent_set: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """Vehicles whose history between ``since`` and ``until`` may have been missed."""

    vehicle_ids: tuple[int, ...]
    since: datetime
    until: datetime


class LiveStateCache:
    def __init__(
        self,
        *,
        max_entities: int = DEFAULT_MAX_ENTITIES,
        dedup_window: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_entities <= 10_000:
            raise ValueError("max_entities must be between 1 and 10000")
        self.max_entities = max_entities
        self.dedup_window = dedup_window
        self._monotonic = monotonic
        self._tracks: OrderedDict[int, _VehicleTrack] = OrderedDict()
        self.stats = LiveStats()

    def __len__(self) -> int:
        return len(self._tracks)

    def newest_event_time(self, vehicle_id: int) -> datetime | None:
        track = self._tracks.get(vehicle_id)
        return track.newest_event_time if track else None

    def snapshot(self, vehicle_id: int) -> dict[str, Any] | None:
        track = self._tracks.get(vehicle_id)
        return copy.deepcopy(track.state) if track else None

    def forget(self, vehicle_ids: Iterable[int]) -> None:
        for vid in vehicle_ids:
            self._tracks.pop(vid, None)

    # -- live payloads ---------------------------------------------------------

    def apply(self, payload: Any) -> LiveUpdate | None:
        """Merge one ``vehicle-events`` payload. Returns ``None`` if nothing to deliver."""
        self.stats.payloads += 1
        if not isinstance(payload, Mapping):
            self.stats.rejected += 1
            return None
        vehicle_id = to_int(payload.get("primary_id"))
        if vehicle_id is None:
            self.stats.rejected += 1
            return None
        updates = payload.get("updates")
        update_keys = (
            {u for u in updates if isinstance(u, str)} if isinstance(updates, list) else set()
        )

        track = self._tracks.get(vehicle_id)
        previous = track.state if track else {}
        merged = deep_merge(previous, payload)

        event: TelemetryEvent | None = None
        out_of_order = False
        if "event" in update_keys and isinstance(payload.get("event"), Mapping):
            merged["event"] = self._restored_event(payload, merged)
            try:
                event = TelemetryEvent.from_vehicle_event(merged, source=EventSource.LIVE)
            except ResponseFormatError:
                self.stats.rejected += 1
                return None
            newest = track.newest_event_time if track else None
            if newest and event.event_time and event.event_time < newest:
                out_of_order = True
                # Keep the newer event as the cached baseline.
                merged["event"] = copy.deepcopy(previous.get("event", merged["event"]))

        track = self._store(vehicle_id, merged)
        online = to_bool(_dig(merged, "device", "connection", "online"))

        if event is None:
            if "device" not in update_keys:
                return None
            self.stats.delivered += 1
            return LiveUpdate(vehicle_id, UpdateKind.DEVICE, copy.deepcopy(merged), None, online)

        if not self._remember(track, event):
            self.stats.duplicates_dropped += 1
            return None
        if out_of_order:
            self.stats.out_of_order += 1
        elif event.event_time:
            track.newest_event_time = event.event_time
        self.stats.delivered += 1
        return LiveUpdate(
            vehicle_id, UpdateKind.EVENT, copy.deepcopy(merged), event, online, out_of_order
        )

    # -- REST recovery -----------------------------------------------------------

    def ingest_recovered(self, events: Iterable[TelemetryEvent]) -> list[TelemetryEvent]:
        """Pass REST-recovered events through the same dedup/order bookkeeping.

        Returns only events not already delivered, oldest first. Recovered events do
        not change the compact baseline (they lack device state); they only advance
        the newest-event watermark.
        """
        fresh: list[TelemetryEvent] = []
        epoch = datetime.min.replace(tzinfo=UTC)
        for event in sorted(events, key=lambda e: e.event_time or epoch):
            if event.vehicle_id is None:
                continue
            track = self._tracks.get(event.vehicle_id) or self._store(event.vehicle_id, {})
            if not self._remember(track, event):
                self.stats.duplicates_dropped += 1
                continue
            if event.event_time and (
                track.newest_event_time is None or event.event_time > track.newest_event_time
            ):
                track.newest_event_time = event.event_time
            fresh.append(event)
        return fresh

    def recovery_request(
        self,
        vehicle_ids: Iterable[int],
        *,
        disconnected_at: datetime,
        now: datetime | None = None,
    ) -> RecoveryRequest | None:
        """Window to backfill after a disconnect: from the oldest watermark to now."""
        ids = tuple(sorted(set(vehicle_ids)))
        if not ids:
            return None
        until = now or datetime.now(UTC)
        marks = [self.newest_event_time(v) or disconnected_at for v in ids]
        since = min(min(marks), disconnected_at)
        return RecoveryRequest(ids, since, until) if since < until else None

    def stale_vehicles(self, max_silence_s: float) -> list[int]:
        """Vehicles with no live payload for longer than ``max_silence_s``.

        Silence means "no information", not "stopped": a GPS or network outage
        looks exactly like this.
        """
        now = self._monotonic()
        return [
            vid
            for vid, track in self._tracks.items()
            if track.last_received_monotonic is not None
            and now - track.last_received_monotonic > max_silence_s
        ]

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _restored_event(payload: Mapping[str, Any], merged: Mapping[str, Any]) -> dict[str, Any]:
        new_event = copy.deepcopy(dict(payload["event"]))
        prior = merged.get("event")
        prior_event: Mapping[str, Any] = prior if isinstance(prior, Mapping) else {}
        restore = payload.get("_event_restore")
        for key in restore if isinstance(restore, list) else []:
            if not isinstance(key, str) or key in new_event:
                continue
            if key in prior_event:
                new_event[key] = copy.deepcopy(prior_event[key])
                continue
            latest = _dig(merged, "device", "latest", "data", key)
            if isinstance(latest, Mapping) and "value" in latest:
                new_event[key] = copy.deepcopy(latest["value"])
        return new_event

    def _store(self, vehicle_id: int, state: dict[str, Any]) -> _VehicleTrack:
        track = self._tracks.get(vehicle_id)
        if track is None:
            track = _VehicleTrack(state=state)
            self._tracks[vehicle_id] = track
        else:
            track.state = state
            self._tracks.move_to_end(vehicle_id)
        track.last_received_monotonic = self._monotonic()
        while len(self._tracks) > self.max_entities:
            self._tracks.popitem(last=False)
            self.stats.evicted += 1
        return track

    def _remember(self, track: _VehicleTrack, event: TelemetryEvent) -> bool:
        key = event.dedup_key
        if key in track.recent_set:
            return False
        track.recent_keys.append(key)
        track.recent_set.add(key)
        while len(track.recent_keys) > self.dedup_window:
            track.recent_set.discard(track.recent_keys.popleft())
        return True


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data
