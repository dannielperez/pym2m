# Changelog

## Unreleased

### Added
- `list_groups()` and the `Group` model for scoped-token group discovery.

## 0.1.0 — 2026-10-13

### Added
- Read-only REST client (`M2MClient`): API root, vehicles, devices, latest locations.
- Password login (domain or central auth) and static application-token credentials;
  token sent only via the `Authenticate` header; renewal on 401 and after idle time;
  401 disambiguated into `AuthenticationError` vs `PermissionDeniedError`.
- Transport with explicit timeouts, TLS verification, connection pooling, no
  redirects, bounded jittered retries for idempotent reads, and a 429 policy that
  never hammers the platform.
- Typed errors with secret-free `RequestDiagnostics`.
- `Secret` wrapper and redaction of credentials in errors and diagnostics.
- Typed models (`Vehicle`, `Device`, `LatestLocation`, `GpsFix`, `ConnectionState`)
  that keep the raw payload and never invent GPS accuracy.
- Safe parameter builders and bounded pagination with explicit truncation.
- `validate_live_url` for the server-advertised realtime URL.
- History: `rawdata`, `trips`, `counters`, `vehicle_counters`, with bounded windows
  and entity counts, explicit units, and unified `TelemetryEvent` normalization.
- Reference data: `list_geofences`, `list_triggers`, `alerts`, `reverse_geocode`.
- `pym2m.live`: Engine.IO 3 / Socket.IO 2 codec over `websockets`; `LiveStateCache`
  with documented compact-merge semantics, dedup, ordering and bounded memory;
  `LiveSession` with chunked subscriptions (250 max), ping, reconnect with backoff,
  resubscription, recovery windows and `recover_with_rest`.
- `parse_forwarder_body` for forwarder pushes, with size limits, per-item
  rejection and ack bodies.
- Opt-in interop test against a real `socket.io@2.5.0` server (CI job).
