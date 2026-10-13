# pym2m

A typed, **read-only** Python client for fleet-tracking platforms built on the
Pegasus Gateway API, such as M2M Global deployments.

- Read vehicles, tracking devices, latest positions and connection state.
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

## Semantics you should know

| Topic | Behaviour |
|---|---|
| Timestamps | Always timezone-aware UTC. `event_time` is when the device generated the event; `received_time` is when the server received it. |
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
