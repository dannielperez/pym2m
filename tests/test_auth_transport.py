from __future__ import annotations

import json

import httpx
import pytest

from pym2m import (
    AuthenticationError,
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    PasswordCredentials,
    PermissionDeniedError,
    RateLimitedError,
    ResponseFormatError,
    RetryPolicy,
    Secret,
    ServerError,
    TimeoutError,
    TokenCredentials,
    TransportError,
)
from pym2m.auth import TokenStore
from tests.conftest import (
    PASSWORD,
    SESSION_TOKEN,
    STATIC_TOKEN,
    FakeAPI,
    json_response,
)

ROOT = "/v2/api/"
LOGIN = "/v2/api/login"
VEHICLES = "/v2/api/vehicles"


def login_ok() -> httpx.Response:
    return json_response(
        200, {"message": "User successfully authenticated", "auth": SESSION_TOKEN}
    )


# -- authentication --------------------------------------------------------------


def test_password_login_sends_header_never_url(api: FakeAPI, make_client) -> None:
    api.add("POST", LOGIN, login_ok())
    api.add(
        "GET",
        ROOT,
        json_response(200, {"tag": "7.5.4", "live_url": "https://x.pegasusgateway.com"}),
    )
    client = make_client(password=True)
    info = client.api_info()
    assert info.version == "7.5.4"
    login_req, root_req = api.requests
    assert json.loads(login_req.content) == {"username": "ops@example.test", "password": PASSWORD}
    assert root_req.headers["Authenticate"] == SESSION_TOKEN
    assert SESSION_TOKEN not in str(root_req.url)
    assert "auth" not in root_req.url.params


def test_central_auth_includes_gateway(api: FakeAPI) -> None:
    from pym2m import M2MClient

    api.add("POST", "/", login_ok())
    api.add("GET", ROOT, json_response(200, {"tag": "1"}))
    client = M2MClient(
        "https://tracking.example.test/v2/api/",
        PasswordCredentials("ops@example.test", Secret(PASSWORD), gateway="tracking.example.test"),
        http_transport=api.transport(),
    )
    client.api_info()
    assert api.requests[0].url.host == "auth.pegasusgateway.com"
    assert json.loads(api.requests[0].content)["gateway"] == "tracking.example.test"


def test_failed_login_raises_without_leaking_password(api: FakeAPI, make_client) -> None:
    api.add("POST", LOGIN, json_response(401, {"message": f"bad password {PASSWORD}"}))
    client = make_client(password=True)
    with pytest.raises(AuthenticationError) as excinfo:
        client.api_info()
    assert PASSWORD not in str(excinfo.value)
    assert PASSWORD not in repr(excinfo.value.diagnostics)


def test_login_response_without_token_is_format_error(api: FakeAPI, make_client) -> None:
    api.add("POST", LOGIN, json_response(200, {"message": "ok"}))
    with pytest.raises(ResponseFormatError):
        make_client(password=True).api_info()


def test_expired_session_is_renewed_once(api: FakeAPI, make_client) -> None:
    api.add("POST", LOGIN, login_ok(), login_ok())
    api.add(
        "GET", ROOT, json_response(401, {"message": "expired"}), json_response(200, {"tag": "1"})
    )
    assert make_client(password=True).api_info().version == "1"
    assert api.paths().count(LOGIN) == 2


def test_scope_failure_after_renewal_is_permission_denied(api: FakeAPI, make_client) -> None:
    api.add("POST", LOGIN, login_ok())
    api.add("GET", VEHICLES, json_response(401, {"message": "no scope"}))
    api.add("GET", LOGIN, json_response(200, {"message": "User is logged in / token is valid"}))
    with pytest.raises(PermissionDeniedError):
        make_client(password=True).list_vehicles()


def test_invalid_static_token_raises_authentication_error(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, json_response(401, {"message": "invalid token"}))
    api.add("GET", LOGIN, json_response(401, {"message": "invalid"}))
    with pytest.raises(AuthenticationError) as excinfo:
        make_client().api_info()
    assert not isinstance(excinfo.value, PermissionDeniedError)
    assert STATIC_TOKEN not in str(excinfo.value)
    # A static token is never "renewed" by logging in.
    assert ("POST", LOGIN) not in [(r.method, r.url.path) for r in api.requests]


def test_static_token_with_missing_scope_is_permission_denied(api: FakeAPI, make_client) -> None:
    api.add("GET", VEHICLES, json_response(401, {"message": "no shared group"}))
    api.add("GET", LOGIN, json_response(200, {"message": "valid"}))
    with pytest.raises(PermissionDeniedError):
        make_client().list_vehicles()


def test_idle_token_is_proactively_renewed() -> None:
    now = [0.0]
    store = TokenStore(PasswordCredentials("u", Secret("p")), idle_ttl_s=60, clock=lambda: now[0])
    store.set(Secret("t1"))
    assert store.current() == Secret("t1")
    now[0] = 61
    assert store.current() is None


def test_static_token_is_never_dropped_for_idleness() -> None:
    now = [0.0]
    store = TokenStore(TokenCredentials(Secret("t")), idle_ttl_s=1, clock=lambda: now[0])
    store.touch()
    now[0] = 10_000
    assert store.current() == Secret("t")


def test_credentials_require_secret_wrapper() -> None:
    with pytest.raises(TypeError):
        PasswordCredentials("u", "plain")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TokenCredentials("plain")  # type: ignore[arg-type]


def test_credentials_repr_hides_secrets() -> None:
    creds = PasswordCredentials("u", Secret(PASSWORD))
    assert PASSWORD not in repr(creds)
    assert PASSWORD not in str(creds)


# -- typed HTTP errors -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, BadRequestError), (403, ForbiddenError), (404, NotFoundError), (418, Exception)],
)
def test_status_codes_map_to_typed_errors(api: FakeAPI, make_client, status, error) -> None:
    api.add("GET", ROOT, json_response(status, {"message": "nope"}))
    with pytest.raises(error) as excinfo:
        make_client().api_info()
    assert excinfo.value.diagnostics.status_code == status
    assert excinfo.value.diagnostics.attempts == 1


def test_server_errors_retry_then_raise(api: FakeAPI, make_client, sleeps) -> None:
    api.add("GET", ROOT, json_response(503, {}))
    with pytest.raises(ServerError) as excinfo:
        make_client().api_info()
    assert excinfo.value.diagnostics.attempts == 3
    assert len(sleeps) == 2
    assert all(0 <= s <= 8 for s in sleeps)


def test_server_error_recovers_within_retry_budget(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, json_response(502, {}), json_response(200, {"tag": "ok"}))
    assert make_client().api_info().version == "ok"


def test_retries_can_be_disabled(api: FakeAPI, make_client, sleeps) -> None:
    api.add("GET", ROOT, json_response(500, {}))
    with pytest.raises(ServerError):
        make_client(retry=RetryPolicy(max_retries=0)).api_info()
    assert sleeps == []


def test_unbounded_retry_policies_are_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=50)
    with pytest.raises(ValueError):
        RetryPolicy(max_backoff_s=3600)


def test_timeout_retries_then_raises_typed(api: FakeAPI, make_client, sleeps) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api.add("GET", ROOT, boom)
    with pytest.raises(TimeoutError) as excinfo:
        make_client().api_info()
    assert excinfo.value.diagnostics.attempts == 3


def test_connection_error_is_transport_error(api: FakeAPI, make_client) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    api.add("GET", ROOT, refuse)
    with pytest.raises(TransportError):
        make_client(retry=RetryPolicy(max_retries=0)).api_info()


def test_short_rate_limit_waits_then_retries(api: FakeAPI, make_client, sleeps) -> None:
    api.add(
        "GET",
        ROOT,
        json_response(429, {"message": "Rate exceeded"}, {"Retry-After": "2"}),
        json_response(200, {"tag": "ok"}),
    )
    assert make_client().api_info().version == "ok"
    assert sleeps == [2.0]


def test_long_rate_limit_raises_without_hammering(api: FakeAPI, make_client, sleeps) -> None:
    api.add(
        "GET",
        ROOT,
        json_response(
            429,
            {"message": "Rate exceeded"},
            {
                "X-RateLimit-Reset": "99999999999",
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": "0",
            },
        ),
    )
    with pytest.raises(RateLimitedError) as excinfo:
        make_client().api_info()
    assert excinfo.value.reset_at == 99999999999
    assert excinfo.value.diagnostics.rate_limit.remaining == 0
    assert sleeps == []
    assert len(api.requests) == 1


def test_rate_limit_without_reset_hint_is_not_retried(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, json_response(429, {}))
    with pytest.raises(RateLimitedError):
        make_client().api_info()
    assert len(api.requests) == 1


def test_bad_json_is_format_error(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(ResponseFormatError):
        make_client().api_info()


def test_error_bodies_are_redacted(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, json_response(400, {"echo": f"auth={STATIC_TOKEN}", "token": "zzz"}))
    with pytest.raises(BadRequestError) as excinfo:
        make_client().api_info()
    text = str(excinfo.value)
    assert STATIC_TOKEN not in text
    assert "zzz" not in text


def test_redirects_are_not_followed(api: FakeAPI, make_client) -> None:
    api.add("GET", ROOT, httpx.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(Exception):  # noqa: B017 - any typed error, as long as no hop happens
        make_client().api_info()
    assert len(api.requests) == 1


def test_diagnostics_never_carry_query_strings(api: FakeAPI, make_client) -> None:
    api.add("GET", VEHICLES, json_response(404, {}))
    with pytest.raises(NotFoundError) as excinfo:
        make_client().list_vehicles(search={"name": "Unit"})
    assert excinfo.value.diagnostics.path == VEHICLES


def test_insecure_base_url_rejected() -> None:
    from pym2m import M2MClient, UnsafeURLError

    for url in ("http://x.test/api/", "https://user:pw@x.test/api/", "https://x.test/api/?auth=1"):
        with pytest.raises(UnsafeURLError):
            M2MClient(url, TokenCredentials(Secret("t")))


def test_relative_path_traversal_rejected(make_client) -> None:
    client = make_client()
    for path in ("/vehicles", "../admin", "https://evil.example/"):
        with pytest.raises(ValueError):
            client.raw_get(path)


def test_client_has_no_mutating_methods() -> None:
    from pym2m import M2MClient
    from pym2m.transport import HTTPTransport

    public = {name for name in dir(M2MClient) if not name.startswith("_")}
    assert not {
        n
        for n in public
        if n.split("_")[0] in {"create", "update", "delete", "set", "send", "post", "put"}
    }
    assert not hasattr(HTTPTransport, "post")
    assert not hasattr(HTTPTransport, "put")
    assert not hasattr(HTTPTransport, "delete")
