"""HTTP transport: timeouts, typed errors, bounded retries, authentication.

The transport is read-only by construction: :meth:`HTTPTransport.get` is the only
public data method. The single ``POST`` it issues is the login call, made
internally by the token lifecycle.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from pym2m._version import __version__
from pym2m.auth import PasswordCredentials, TokenStore
from pym2m.diagnostics import RateLimitInfo, RequestDiagnostics
from pym2m.exceptions import (
    APIError,
    AuthenticationError,
    BadRequestError,
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
)
from pym2m.normalize import to_float, to_int
from pym2m.redaction import Secret, redact_text, safe_path
from pym2m.urls import DEFAULT_CENTRAL_AUTH_URL, normalize_base_url

logger = logging.getLogger("pym2m")

AUTH_HEADER = "Authenticate"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_ERROR_BODY = 300


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry for idempotent reads.

    ``max_retries`` extra attempts after the first, exponential backoff with full
    jitter capped at ``max_backoff_s``. A 429 is retried only when the platform's
    reported reset is at most ``max_rate_limit_wait_s`` away; otherwise it raises
    immediately, because hammering after a 429 can get the caller's IP banned.
    """

    max_retries: int = 2
    backoff_base_s: float = 0.5
    max_backoff_s: float = 8.0
    max_rate_limit_wait_s: float = 5.0

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if self.max_backoff_s > 60 or self.max_rate_limit_wait_s > 60:
            raise ValueError("backoff ceilings above 60s are not allowed")


def _rate_limit_info(headers: httpx.Headers) -> RateLimitInfo:
    return RateLimitInfo(
        limit=to_int(headers.get("X-RateLimit-Limit")),
        remaining=to_int(headers.get("X-RateLimit-Remaining")),
        reset_at=to_float(headers.get("X-RateLimit-Reset")),
    )


class HTTPTransport:
    """Owns one pooled :class:`httpx.Client` and the token lifecycle."""

    def __init__(
        self,
        base_url: str,
        token_store: TokenStore,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        verify: bool | str = True,
        retry: RetryPolicy | None = None,
        central_auth_url: str = DEFAULT_CENTRAL_AUTH_URL,
        http_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        user_agent: str | None = None,
    ) -> None:
        if verify is False:
            logger.warning("pym2m: TLS certificate verification is disabled")
        self.base_url = normalize_base_url(base_url)
        self.central_auth_url = normalize_base_url(central_auth_url)
        self.tokens = token_store
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(
            timeout=timeout,
            verify=verify,
            transport=http_transport,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent or f"pym2m/{__version__}",
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    # -- authentication ----------------------------------------------------------

    def login(self) -> Secret:
        """Log in with password credentials and store the session token."""
        creds = self.tokens.credentials
        if not isinstance(creds, PasswordCredentials):
            raise AuthenticationError("static token credentials cannot log in")
        body: dict[str, str] = {"username": creds.username, "password": creds.password.reveal()}
        url = self.base_url + "login"
        if creds.gateway:
            body["gateway"] = creds.gateway
            url = self.central_auth_url
        started = self._clock()
        try:
            response = self._client.post(url, json=body)
        except httpx.TimeoutException:
            diag = self._diag("POST", url, None, 1, started)
            raise TimeoutError("login timed out", diagnostics=diag) from None
        except httpx.HTTPError as exc:
            raise TransportError(
                f"login transport failure: {type(exc).__name__}",
                diagnostics=self._diag("POST", url, None, 1, started),
            ) from None
        diag = self._diag("POST", url, response, 1, started)
        if response.status_code in (400, 401, 403):
            raise AuthenticationError("login rejected by the platform", diagnostics=diag)
        if response.status_code != 200:
            raise self._error_for(response, diag)
        payload = self._decode(response, diag)
        token = payload.get("auth") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise ResponseFormatError("login response has no 'auth' token", diagnostics=diag)
        secret = Secret(token)
        self.tokens.set(secret)
        logger.info("pym2m: authenticated (session token obtained)")
        return secret

    def _token(self) -> Secret:
        with self.tokens.lock:
            token = self.tokens.current()
            if token is None:
                token = self.login()
            return token

    # -- requests ------------------------------------------------------------------

    def get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """GET ``base_url + path`` and return decoded JSON.

        Retries transient failures per :class:`RetryPolicy`. On a 401 with
        renewable credentials, logs in again once; a second 401 raises
        :class:`PermissionDeniedError`.
        """
        if path.startswith(("/", "http:", "https:")) or ".." in path:
            raise ValueError("path must be relative to the base URL")
        url = self.base_url + path
        renewed = False
        while True:
            token = self._token()
            try:
                return self._get_with_retries(url, params, token)
            except AuthenticationError:
                if self.tokens.renewable and not renewed:
                    self.tokens.clear()
                    renewed = True
                    continue
                if self._token_is_valid(token):
                    raise PermissionDeniedError(
                        "token is valid but lacks the scope or group for this resource",
                        diagnostics=None,
                    ) from None
                raise

    def _token_is_valid(self, token: Secret) -> bool:
        """``GET login`` reports whether the token is still a live session."""
        try:
            response = self._client.get(
                self.base_url + "login", headers={AUTH_HEADER: token.reveal()}
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _get_with_retries(self, url: str, params: Mapping[str, str] | None, token: Secret) -> Any:
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(
                    url, params=dict(params or {}), headers={AUTH_HEADER: token.reveal()}
                )
            except httpx.TimeoutException:
                diag = self._diag("GET", url, None, attempt, started)
                if attempt <= self.retry.max_retries:
                    self._backoff(attempt)
                    continue
                raise TimeoutError("request timed out", diagnostics=diag) from None
            except httpx.HTTPError as exc:
                diag = self._diag("GET", url, None, attempt, started)
                if attempt <= self.retry.max_retries:
                    self._backoff(attempt)
                    continue
                raise TransportError(
                    f"transport failure: {type(exc).__name__}", diagnostics=diag
                ) from None

            diag = self._diag("GET", url, response, attempt, started)
            status = response.status_code
            if status == 200:
                self.tokens.touch()
                return self._decode(response, diag)
            if status == 429:
                wait = self._rate_limit_wait(response)
                if (
                    attempt <= self.retry.max_retries
                    and wait is not None
                    and wait <= self.retry.max_rate_limit_wait_s
                ):
                    self._sleep(max(wait, 0.0))
                    continue
                raise RateLimitedError(
                    "rate limited by the platform",
                    reset_at=diag.rate_limit.reset_at,
                    diagnostics=diag,
                )
            if status in _RETRYABLE_STATUS and attempt <= self.retry.max_retries:
                self._backoff(attempt)
                continue
            raise self._error_for(response, diag)

    # -- helpers ---------------------------------------------------------------------

    def _rate_limit_wait(self, response: httpx.Response) -> float | None:
        retry_after = to_float(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after
        reset = to_float(response.headers.get("X-RateLimit-Reset"))
        if reset is None:
            return None
        return reset - self._clock()

    def _backoff(self, attempt: int) -> None:
        ceiling = min(self.retry.max_backoff_s, self.retry.backoff_base_s * (2 ** (attempt - 1)))
        self._sleep(random.uniform(0, ceiling))

    def _diag(
        self,
        method: str,
        url: str,
        response: httpx.Response | None,
        attempts: int,
        started: float,
    ) -> RequestDiagnostics:
        headers = response.headers if response is not None else httpx.Headers()
        return RequestDiagnostics(
            method=method,
            path=safe_path(url),
            status_code=response.status_code if response is not None else None,
            attempts=attempts,
            elapsed_s=round(max(self._clock() - started, 0.0), 3),
            rate_limit=_rate_limit_info(headers),
            cache_status=headers.get("x-cache-status"),
            server_id=headers.get("X-Peg-Server"),
        )

    def _decode(self, response: httpx.Response, diag: RequestDiagnostics) -> Any:
        try:
            return json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            raise ResponseFormatError(
                "response body is not valid JSON", diagnostics=diag
            ) from None

    def _error_for(self, response: httpx.Response, diag: RequestDiagnostics) -> M2MError:
        detail = redact_text(response.text[:_MAX_ERROR_BODY], *self.tokens.secrets())
        status = response.status_code
        message = f"HTTP {status} from {diag.path}: {detail}".strip()
        mapping: dict[int, type[APIError]] = {
            400: BadRequestError,
            401: AuthenticationError,
            403: ForbiddenError,
            404: NotFoundError,
            409: ConflictError,
        }
        if status in mapping:
            return mapping[status](message, diagnostics=diag)
        if status >= 500:
            return ServerError(message, status_code=status, diagnostics=diag)
        return APIError(message, status_code=status, diagnostics=diag)
