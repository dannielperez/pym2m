# Forwarder parser boundary

`parse_forwarder_body` accepts flat `json_fwd` telemetry and the nested
`vehicle-events` wrapper already supported by the SDK. It normalizes UTC event
and receive timestamps, preserves source speed units, reports invalid GPS as an
unusable fix, and keeps unknown event fields in `TelemetryEvent.raw` (excluded
from repr). A positive event ID, identity and event timestamp are required.

The parser limits body size, event count and nesting, including predecoded
inputs. Non-JSON values fail the whole batch. Individual malformed events have
safe rejection reasons. Identical event IDs collapse and increment `duplicates`;
conflicting data under one ID fails the batch. Cross-request and cross-transport
idempotency must be enforced by the receiver using an account namespace and the
existing event deduplication key.

`devicepkg` carries device state, not this event contract. It is not converted to
GPS telemetry. Obtain actual samples and a separately verified contract before
supporting it. The parser does not authenticate HTTP, authorize groups or duty
windows, or persist data. Treat `raw` as sensitive and never log it.

The [public Pegasus Forwarder documentation](https://pegasus.digitalcomtech.com/docs/forwarders)
describes an empty acknowledgment array for success and IDs for retry. This is
not confirmation of any particular operator's acknowledgment, retry, signing,
ordering or cost contract. `ack_body` validates supplied retry IDs belong to
parsed events; it cannot determine durability. Call it only after accepted
items are durably queued/persisted and rejected items have been durably handled
under the receiver's bounded rejection policy. Do not acknowledge transient
queue/storage failures. Confirm provider semantics before enabling ingress.

Synthetic tests cover 31 and 125 distinct vehicles, duplicate delivery,
conflicting identities, missing IDs, malformed timestamps, invalid GPS,
non-JSON/nested/oversized bodies and malformed acknowledgments. They do not prove
real Forwarder delivery or provider authentication.
