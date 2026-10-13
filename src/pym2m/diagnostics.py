"""Secret-free request diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    """Values of the ``X-RateLimit-*`` response headers, when the platform sends them."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None


@dataclass(frozen=True, slots=True)
class RequestDiagnostics:
    """What happened on one logical request (across retries).

    Contains no credentials and no query string: ``path`` is the URL path only.
    """

    method: str
    path: str
    status_code: int | None = None
    attempts: int = 1
    elapsed_s: float = 0.0
    rate_limit: RateLimitInfo = field(default_factory=RateLimitInfo)
    cache_status: str | None = None
    server_id: str | None = None
