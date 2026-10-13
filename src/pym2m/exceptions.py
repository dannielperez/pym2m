"""Typed error hierarchy.

Every error carries optional :class:`~pym2m.diagnostics.RequestDiagnostics` that is
safe to log: it never contains passwords, tokens or full query strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pym2m.diagnostics import RequestDiagnostics


class M2MError(Exception):
    """Base class for every error raised by pym2m."""

    def __init__(self, message: str, *, diagnostics: RequestDiagnostics | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class ConfigurationError(M2MError):
    """The client was configured with unsafe or invalid settings."""


class UnsafeURLError(ConfigurationError):
    """A base URL or server-advertised URL failed validation."""


class TransportError(M2MError):
    """The request never produced an HTTP response (DNS, TLS, connect, read)."""


class TimeoutError(TransportError):  # noqa: A001 - deliberate, namespaced under pym2m
    """The request exceeded its configured timeout."""


class APIError(M2MError):
    """The platform returned an HTTP error status."""

    status_code: int = 0

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        diagnostics: RequestDiagnostics | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        if status_code is not None:
            self.status_code = status_code


class BadRequestError(APIError):
    """HTTP 400 - invalid or malformed parameters."""

    status_code = 400


class AuthenticationError(APIError):
    """Login failed, or the token is invalid/expired (HTTP 401 on login or after renewal)."""

    status_code = 401


class PermissionDeniedError(APIError):
    """HTTP 401 on a resource for a token that is otherwise valid.

    The platform uses 401 both for an invalid token and for a token whose scopes or
    groups do not cover the requested entity. pym2m raises this subclass when a
    renewed token still receives 401, i.e. the failure is a scope/group problem.
    """

    status_code = 401


class ForbiddenError(APIError):
    """HTTP 403 - operation not allowed regardless of scopes (e.g. token limit reached)."""

    status_code = 403


class NotFoundError(APIError):
    """HTTP 404 - resource missing, or masked by the platform for security reasons."""

    status_code = 404


class ConflictError(APIError):
    """HTTP 409."""

    status_code = 409


class RateLimitedError(APIError):
    """HTTP 429. ``reset_at`` is the epoch second at which the limit resets, if reported.

    Continuing to send requests after a 429 can get the caller's IP address banned, so
    pym2m only retries a 429 when the reported reset falls inside the configured
    backoff ceiling.
    """

    status_code = 429

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        diagnostics: RequestDiagnostics | None = None,
    ) -> None:
        super().__init__(message, status_code=429, diagnostics=diagnostics)
        self.reset_at = reset_at


class ServerError(APIError):
    """HTTP 5xx."""

    status_code = 500


class ResponseFormatError(M2MError):
    """The response body was not valid JSON or did not have the documented shape."""


class UnsupportedResponseError(ResponseFormatError):
    """The platform answered with a documented-but-unsupported shape (e.g. an async job)."""
