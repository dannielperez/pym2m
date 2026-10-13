# pym2m

A typed, **read-only** Python client for fleet-tracking platforms built on the
Pegasus Gateway API, such as M2M Global deployments.

- Read vehicles, tracking devices, latest positions and connection state.
- Historical telemetry (rawdata), trips, counters, geofences, triggers, alerts and
  reverse geocoding, over bounded time windows.
- A realtime session (`pym2m[live]`) that reconstructs compact deltas, deduplicates,
  orders, reconnects, re-subscribes and tells you which window to backfill.
- A parser for forwarder (HTTP push) payloads.
- Explicit timeouts, TLS verification, bounded retries and a 429 policy that
  backs off rather than hammering the API.
- Credentials are wrapped in `Secret` and redacted from every error and diagnostic.
- No method changes platform state. Remote commands (outputs, immobilization,
  firmware, console, configuration) are deliberately not implemented.

pym2m has no dependency on any web framework or application. It needs Python 3.11+
and `httpx`.

> **Status: alpha, not yet verified against a live account.** Everything is tested
> offline against synthetic payloads modelled on the platform's public developer
> documentation. See [docs/PROVENANCE.md](docs/PROVENANCE.md) for what is verified
> and how.

pym2m is an independent project. It is not affiliated with or endorsed by
Digital Communications Technologies or any platform reseller.

## Install

```bash
pip install "pym2m @ git+https://github.com/dannielperez/pym2m@<tag>"
```

## Quick start

```python
import os

from pym2m import M2MClient, Secret, TokenCredentials

# Prefer a finite, read-only, group-scoped application token.
creds = TokenCredentials(Secret(os.environ["TRACKING_API_TOKEN"]))

with M2MClient("https://tracking.example.com/api/", creds, timeout=10) as client:
    for vehicle in client.latest_locations(groups=[123]).items:
        loc = vehicle.latest_location
        if loc is None or not loc.fix.is_usable:
            print(vehicle.id, "no usable position", loc and loc.fix.issue)
            continue
        print(
            vehicle.id,
            vehicle.name,
            loc.fix.lat,
            loc.fix.lon,
            f"{loc.speed.kph:.0f} km/h" if loc.speed else "speed unknown",
            loc.event_time.isoformat() if loc.event_time else "time unknown",
        )
```

Password login is also supported. pym2m renews the session token after a 401 or
after a configurable idle period:

```python
from pym2m import PasswordCredentials

creds = PasswordCredentials("integration@example.com", Secret(os.environ["TRACKING_PASSWORD"]))
```

## History

```python
from datetime import UTC, datetime, timedelta

end = datetime.now(UTC)
start = end - timedelta(hours=2)
events = client.rawdata([vehicle_id], start, end)   # TelemetryEvent, oldest first
trips = client.trips([vehicle_id], start, end)      # meters / seconds / km/h
totals = client.counters([vehicle_id], start, end)  # e.g. totals[0].values["dev_dist"]
```

Windows are capped by `max_history_window` (24 hours by default, 31 days at most)
and history queries take at most 50 entities, because these endpoints are rate
limited.

## Realtime

```bash
pip install "pym2m[live] @ git+https://github.com/dannielperez/pym2m@<tag>"
```

```python
import threading

from pym2m.live import LiveSession, recover_with_rest

info = client.api_info()
stop = threading.Event()

def on_update(update):          # called on the session thread: keep it quick
    queue.put(update)

def on_recovery_needed(request):
    # Backfill what may have been missed while disconnected.
    for event in recover_with_rest(client, request, session.cache):
        queue.put(event)

session = LiveSession(
    info.live_url,                              # validated against an allow-list
    site_url="https://tracking.example.com",
    token=lambda: creds.token,
    on_update=on_update,
    on_recovery_needed=on_recovery_needed,
)
session.subscribe([101, 102])                   # at most 250 vehicles
session.run(stop)                               # blocks; reconnects with backoff
```

The realtime server needs a Socket.IO **2.x** client over WebSocket. Socket.IO 2.x
speaks Engine.IO protocol 3, which current python-socketio releases do not
support. pym2m ships a small Engine.IO 3 codec over the maintained `websockets`
library. CI runs it against a real `socket.io@2.5.0` server with synthetic data
(`tests/test_interop_sio2.py`).

**Compact merge semantics.** Objects merge recursively, while arrays and scalars
replace. A new event replaces the previous event, and the keys listed in
`_event_restore` are copied back from the previous event or from
`device.latest.data`. Falsy values such as `false` and `0` are restored too.
Repeated payloads are not delivered twice. Older events arrive flagged
`out_of_order=True` and never overwrite newer state.

## Forwarder payloads

```python
from pym2m import parse_forwarder_body

batch = parse_forwarder_body(request_body_bytes)   # size- and count-limited
for event in batch.events:
    ...
response_json = batch.ack_body(failed_event_ids)   # [] acknowledges everything
```

pym2m does not provide an HTTP endpoint and does not authenticate senders. Agree
on request authentication, source allow-listing and replay protection with the
platform operator before you expose a receiver.

## Semantics you should know

| Topic | Behaviour |
|---|---|
| Timestamps | Always timezone-aware UTC. `event_time` is when the device generated the event; `received_time` is when the server received it. |
| Delivery | `TelemetryEvent.dedup_key` is stable across REST, realtime and forwarder delivery of the same event. |
| Silence | No events means *no information*, not "stopped". `LiveStateCache.stale_vehicles()` reports silent vehicles. |
| Positions | `GpsFix.is_usable` is false for missing, non-numeric, out-of-range, `(0, 0)`, or device-flagged-invalid fixes. |
| GPS validity | `device_valid` is the tracker's own flag. A valid fix is **not** proof of precise presence. |
| Accuracy | `accuracy_m` is always `None`: no accuracy in metres is documented. HDOP/PDOP are kept as unitless hints. |
| Speed | `Speed` keeps the source unit (the documented `mph` field) and converts on demand. |
| Unknown fields | Every model keeps the provider payload in `raw`. Unpromoted fields carry no implied meaning. |
| Pagination | Bounded by `max_pages`; `PagedResult.truncated` tells you when a walk stopped early. |
| 401 | Raised as `AuthenticationError` (bad or expired credentials) or `PermissionDeniedError` (valid token without the scope or group). |
| 429 | Retried only when the reported reset is within `RetryPolicy.max_rate_limit_wait_s`; otherwise `RateLimitedError(reset_at=...)` is raised immediately. |

## Security

- Tokens travel only in the `Authenticate` header, never in URLs.
- Credentials must be wrapped in `Secret`, whose `repr`/`str` never reveal the value
  and which refuses to pickle.
- Error messages and `RequestDiagnostics` carry the URL path only (no query string)
  and are passed through credential redaction.
- Redirects are not followed. Base URLs must be `https` with no embedded
  credentials.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests && mypy && pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE). The license covers this code only.
