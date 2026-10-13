"""Credentials and token lifecycle.

Two credential kinds are supported:

* :class:`PasswordCredentials` - pym2m logs in, receives a session token and renews
  it after a 401 or after the configured idle period. Session tokens expire after
  a period of inactivity (60 minutes by default per public platform docs).
* :class:`TokenCredentials` - a pre-issued token, ideally a finite, read-only,
  group-scoped application token. pym2m never renews it; a rejected token raises.

The token is sent only in the ``Authenticate`` request header, never in a URL.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pym2m.redaction import Secret


@dataclass(frozen=True, slots=True)
class PasswordCredentials:
    """Username/password login.

    ``gateway`` selects central authentication: pym2m posts to the central auth
    URL with ``gateway=<host>`` instead of ``<base_url>login``.
    """

    username: str
    password: Secret
    gateway: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.password, Secret):
            raise TypeError("password must be wrapped in pym2m.Secret")
        if not self.username:
            raise ValueError("username is required")


@dataclass(frozen=True, slots=True)
class TokenCredentials:
    """A pre-issued session or application token."""

    token: Secret

    def __post_init__(self) -> None:
        if not isinstance(self.token, Secret):
            raise TypeError("token must be wrapped in pym2m.Secret")


Credentials = PasswordCredentials | TokenCredentials


class TokenStore:
    """Holds the current token and decides when it must be renewed. Thread-safe."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        idle_ttl_s: float = 55 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.idle_ttl_s = idle_ttl_s
        self._clock = clock
        self.lock = threading.RLock()
        self._token: Secret | None = (
            credentials.token if isinstance(credentials, TokenCredentials) else None
        )
        self._last_used: float | None = None
        self.obtained_by_login = False

    @property
    def renewable(self) -> bool:
        return isinstance(self.credentials, PasswordCredentials)

    def current(self) -> Secret | None:
        with self.lock:
            if (
                self.renewable
                and self._token is not None
                and self._last_used is not None
                and self._clock() - self._last_used > self.idle_ttl_s
            ):
                # Likely expired server-side through inactivity; force a fresh login.
                self._token = None
            return self._token

    def set(self, token: Secret) -> None:
        with self.lock:
            self._token = token
            self._last_used = self._clock()
            self.obtained_by_login = True

    def touch(self) -> None:
        with self.lock:
            self._last_used = self._clock()

    def clear(self) -> None:
        with self.lock:
            if self.renewable:
                self._token = None

    def secrets(self) -> tuple[Secret | None, ...]:
        """Everything that must be scrubbed from diagnostics."""
        password = (
            self.credentials.password
            if isinstance(self.credentials, PasswordCredentials)
            else None
        )
        return (self._token, password)
