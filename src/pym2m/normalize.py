"""Value normalization shared by the REST, live and forwarder parsers.

Rules:

* Timestamps become timezone-aware UTC ``datetime`` objects. The platform reports
  epochs (seconds) in live/forwarder payloads and naive ``YYYY-MM-DDThh:mm:ss``
  strings in REST payloads; REST timestamps are UTC unless the caller asked for a
  different ``tz`` (pym2m never does).
* Coordinates are validated for range and finiteness. ``(0, 0)`` is reported as
  invalid ("null island"), because trackers emit it when they have no fix.
* Speeds keep their source unit. :class:`Speed` converts on demand.
* Unknown values stay ``None``. In particular there is no accuracy-in-metres field
  in the documented payloads; DOP values are unitless quality hints, not accuracy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

MPH_TO_KPH = 1.609344
MPH_TO_MPS = 0.44704

# Epochs before 2000 or more than ~10 years ahead are treated as corrupt.
_MIN_EPOCH = 946_684_800
_MAX_EPOCH_SKEW_S = 10 * 365 * 24 * 3600


class SpeedUnit(StrEnum):
    MPH = "mph"
    KPH = "kph"
    MPS = "m/s"


@dataclass(frozen=True, slots=True)
class Speed:
    """A speed in its source unit."""

    value: float
    unit: SpeedUnit

    @property
    def kph(self) -> float:
        if self.unit is SpeedUnit.KPH:
            return self.value
        if self.unit is SpeedUnit.MPH:
            return self.value * MPH_TO_KPH
        return self.value * 3.6

    @property
    def mph(self) -> float:
        if self.unit is SpeedUnit.MPH:
            return self.value
        return self.kph / MPH_TO_KPH


class PositionIssue(StrEnum):
    MISSING = "missing"
    NOT_NUMERIC = "not_numeric"
    OUT_OF_RANGE = "out_of_range"
    NULL_ISLAND = "null_island"
    DEVICE_FLAGGED_INVALID = "device_flagged_invalid"


def to_float(value: Any) -> float | None:
    """Coerce numbers and numeric strings; reject bools, NaN and infinities."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def to_bool(value: Any) -> bool | None:
    """Strict tri-state boolean: unknown stays ``None`` rather than ``False``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return None


def parse_timestamp(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Parse an epoch (s) or a naive/aware ISO-8601 string into aware UTC.

    Returns ``None`` for missing or implausible values instead of guessing.
    """
    if value is None or isinstance(value, bool):
        return None
    reference = now or datetime.now(UTC)
    if isinstance(value, int | float):
        epoch = float(value)
        if not math.isfinite(epoch):
            return None
        # Millisecond epochs are not documented; accept them only if unambiguous.
        if epoch > 1e12:
            epoch /= 1000.0
        if epoch < _MIN_EPOCH or epoch > reference.timestamp() + _MAX_EPOCH_SKEW_S:
            return None
        return datetime.fromtimestamp(epoch, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        numeric = to_float(text)
        if numeric is not None:
            return parse_timestamp(numeric, now=reference)
        text = text.replace(" ", "T", 1) if "T" not in text else text
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        if parsed.timestamp() < _MIN_EPOCH:
            return None
        return parsed
    return None


def validate_coordinates(lat: Any, lon: Any) -> tuple[float, float] | PositionIssue:
    """Return ``(lat, lon)`` or the reason the pair is unusable."""
    if lat is None or lon is None:
        return PositionIssue.MISSING
    flat, flon = to_float(lat), to_float(lon)
    if flat is None or flon is None:
        return PositionIssue.NOT_NUMERIC
    if not (-90.0 <= flat <= 90.0 and -180.0 <= flon <= 180.0):
        return PositionIssue.OUT_OF_RANGE
    if flat == 0.0 and flon == 0.0:
        return PositionIssue.NULL_ISLAND
    return flat, flon


def format_api_datetime(value: datetime) -> str:
    """Render a datetime as the platform's naive-UTC ``YYYY-MM-DDThh:mm:ss`` form."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass a timezone-aware datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
