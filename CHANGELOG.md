# Changelog

## Unreleased

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
