"""Safe query-parameter construction.

All user-supplied values are validated before they reach a URL so a caller cannot
inject extra parameters (for example ``auth=``) through a field name or search key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from pym2m.normalize import format_api_datetime

_FIELD_RE = re.compile(r"^[A-Za-z0-9_$.:*\-]+$")
_SEARCH_KEY_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_$]+)*$")
_FORBIDDEN_PARAM_NAMES = frozenset({"auth", "authenticate", "token"})
MAX_IDS_PER_REQUEST = 500


def id_list(values: Iterable[int], *, name: str = "ids") -> str:
    """Comma-join positive integer ids, de-duplicated, order preserved."""
    seen: dict[int, None] = {}
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must contain positive integers")
        seen[value] = None
    if not seen:
        raise ValueError(f"{name} must not be empty")
    if len(seen) > MAX_IDS_PER_REQUEST:
        raise ValueError(f"{name} accepts at most {MAX_IDS_PER_REQUEST} ids per request")
    return ",".join(str(v) for v in seen)


def field_list(fields: Iterable[str], *, name: str = "fields") -> str:
    """Comma-join field selectors such as ``lat``, ``device:latest.loc`` or ``$basic``."""
    items = list(fields)
    if not items:
        raise ValueError(f"{name} must not be empty")
    for item in items:
        if not isinstance(item, str) or not _FIELD_RE.match(item):
            raise ValueError(f"invalid {name} entry: {item!r}")
    return ",".join(items)


def search_params(search: dict[str, str | int] | None) -> dict[str, str]:
    """Build ``search.<path>=<value>`` params. Matching is case-sensitive server-side."""
    params: dict[str, str] = {}
    for key, value in (search or {}).items():
        if not _SEARCH_KEY_RE.match(key) or key.split(".", 1)[0].lower() in _FORBIDDEN_PARAM_NAMES:
            raise ValueError(f"invalid search key: {key!r}")
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise ValueError("search values must be str or int")
        params[f"search.{key}"] = str(value)
    return params


def time_window(start: datetime, end: datetime, *, max_window: timedelta) -> dict[str, str]:
    """Validate and render a bounded ``from``/``to`` window."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > max_window:
        raise ValueError(f"time window exceeds the configured maximum of {max_window}")
    return {"from": format_api_datetime(start), "to": format_api_datetime(end)}
