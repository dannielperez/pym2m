"""Validation for configured and server-advertised URLs."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from pym2m.exceptions import UnsafeURLError

# Hosts that may serve the realtime socket. Public Pegasus documentation states that
# any sub-domain of these two domains is a legitimate platform host.
DEFAULT_LIVE_HOST_SUFFIXES: tuple[str, ...] = ("pegasusgateway.com", "peginstances.com")
DEFAULT_CENTRAL_AUTH_URL = "https://auth.pegasusgateway.com/"


def normalize_base_url(url: str) -> str:
    """Validate an API base URL and return it with exactly one trailing slash.

    HTTPS only, no embedded credentials, no query string or fragment.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeURLError("base URL must use https")
    if not parts.hostname:
        raise UnsafeURLError("base URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("base URL must not embed credentials")
    if parts.query or parts.fragment:
        raise UnsafeURLError("base URL must not carry a query string or fragment")
    path = parts.path.rstrip("/") + "/"
    return urlunsplit(("https", parts.netloc, path, "", ""))


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def validate_live_url(
    url: object,
    *,
    allowed_host_suffixes: tuple[str, ...] = DEFAULT_LIVE_HOST_SUFFIXES,
) -> str:
    """Validate the ``live_url`` advertised by the API root before connecting to it.

    The value comes from the server, so it is treated as untrusted: it must be an
    ``https`` URL on an allow-listed host, on the default port, with no credentials,
    path, query or fragment. Returns the normalized origin (``https://host``).
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("live_url missing from API root response")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeURLError("live_url must use https")
    if not parts.hostname or not _host_matches(parts.hostname, allowed_host_suffixes):
        raise UnsafeURLError("live_url host is not in the allowed host list")
    if parts.username or parts.password:
        raise UnsafeURLError("live_url must not embed credentials")
    if parts.port not in (None, 443):
        raise UnsafeURLError("live_url must use the default https port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise UnsafeURLError("live_url must be a bare origin")
    return f"https://{parts.hostname.lower()}"
