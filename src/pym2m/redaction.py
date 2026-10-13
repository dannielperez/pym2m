"""Credential redaction helpers.

Every string pym2m puts into an exception message, a log record or a diagnostics
object passes through :func:`redact_text`. Secrets are also wrapped in
:class:`Secret`, whose ``repr``/``str`` never reveal the value, so an accidental
``print(credentials)`` or a traceback with local variables does not leak them.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

REDACTED: Final = "***"

# Query-string or JSON-ish key/value pairs whose value must never be shown.
_SENSITIVE_KEYS: Final = ("auth", "token", "password", "authenticate", "secret", "api_key")
_PAIR_RE: Final = re.compile(
    r"(?i)(?P<key>[\"']?(?:" + "|".join(_SENSITIVE_KEYS) + r")[\"']?\s*[=:]\s*[\"']?)"
    r"(?P<value>[^\"'&,;\s}]+)"
)


class Secret:
    """An opaque wrapper for a credential. ``reveal()`` is the only way to read it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("pym2m.Secret", self._value))

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("pym2m.Secret cannot be pickled")


def redact_text(text: str, *secrets: str | Secret | None) -> str:
    """Remove known secret values and ``key=value`` credential pairs from ``text``."""
    for secret in secrets:
        if secret is None:
            continue
        raw = secret.reveal() if isinstance(secret, Secret) else secret
        if raw:
            text = text.replace(raw, REDACTED)
    return _PAIR_RE.sub(lambda m: m.group("key") + REDACTED, text)


def safe_path(url_or_path: str) -> str:
    """Return the path component only, so diagnostics never carry query strings."""
    return urlsplit(url_or_path).path or "/"
