"""Shared fixtures. Every payload here is synthetic; no test talks to a real platform."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pym2m import M2MClient, PasswordCredentials, RetryPolicy, Secret, TokenCredentials

BASE_URL = "https://tracking.example.test/v2/api/"
SESSION_TOKEN = "synthetic-session-token-0001"
STATIC_TOKEN = "synthetic-app-token-0002"
PASSWORD = "synthetic-password-0003"

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


class FakeAPI:
    """Route table keyed by (method, path). Values are handlers or queues of responses."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Handler | httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path: str, *responses: Handler | httpx.Response) -> None:
        self.routes.setdefault((method, path), []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return json_response(404, {"message": "no synthetic route"})
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        return item(request) if callable(item) else item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_client(api: FakeAPI, sleeps: list[float]) -> Callable[..., M2MClient]:
    def _make(*, password: bool = False, **kwargs: Any) -> M2MClient:
        creds = (
            PasswordCredentials("ops@example.test", Secret(PASSWORD))
            if password
            else TokenCredentials(Secret(STATIC_TOKEN))
        )
        kwargs.setdefault("retry", RetryPolicy(max_retries=2, max_rate_limit_wait_s=5))
        return M2MClient(
            BASE_URL, creds, http_transport=api.transport(), sleep=sleeps.append, **kwargs
        )

    return _make


def vehicle_payload(vid: int, *, imei: int = 350000000000001, **loc: Any) -> dict[str, Any]:
    latest = {
        "lat": 18.40,
        "lon": -66.05,
        "mph": 10.0,
        "valid": True,
        "evtime": 1_760_000_000,
        "systime": 1_760_000_003.5,
        "head": 90,
        "moving": True,
        "code": 1,
        "label": "trckpnt",
    }
    latest.update(loc)
    return {
        "id": vid,
        "name": f"Unit {vid}",
        "groups": [10, 11],
        "info": {"make": "ExampleMake", "license_plate": "TEST-001", "year": 2024},
        "device": {
            "imei": imei,
            "latest": {"loc": latest},
            "connection": {"online": True, "_epoch": 1_760_000_001},
        },
    }
