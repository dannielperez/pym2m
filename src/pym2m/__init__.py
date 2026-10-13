"""pym2m - read-only client for Pegasus-Gateway-based fleet tracking platforms."""

from pym2m._version import __version__
from pym2m.auth import PasswordCredentials, TokenCredentials
from pym2m.client import M2MClient
from pym2m.diagnostics import RateLimitInfo, RequestDiagnostics
from pym2m.exceptions import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    ForbiddenError,
    M2MError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ResponseFormatError,
    ServerError,
    TimeoutError,
    TransportError,
    UnsafeURLError,
    UnsupportedResponseError,
)
from pym2m.models import (
    ApiInfo,
    ConnectionState,
    Device,
    GpsFix,
    LatestLocation,
    PagedResult,
    Vehicle,
    VehicleInfo,
)
from pym2m.normalize import PositionIssue, Speed, SpeedUnit
from pym2m.redaction import Secret
from pym2m.transport import RetryPolicy
from pym2m.urls import validate_live_url

__all__ = [
    "APIError",
    "ApiInfo",
    "AuthenticationError",
    "BadRequestError",
    "ConfigurationError",
    "ConflictError",
    "ConnectionState",
    "Device",
    "ForbiddenError",
    "GpsFix",
    "LatestLocation",
    "M2MClient",
    "M2MError",
    "NotFoundError",
    "PagedResult",
    "PasswordCredentials",
    "PermissionDeniedError",
    "PositionIssue",
    "RateLimitInfo",
    "RateLimitedError",
    "RequestDiagnostics",
    "ResponseFormatError",
    "RetryPolicy",
    "Secret",
    "ServerError",
    "Speed",
    "SpeedUnit",
    "TimeoutError",
    "TokenCredentials",
    "TransportError",
    "UnsafeURLError",
    "UnsupportedResponseError",
    "Vehicle",
    "VehicleInfo",
    "__version__",
    "validate_live_url",
]
