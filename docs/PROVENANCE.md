# Source provenance and publication status

## What this package is built from

pym2m implements a client for the **Pegasus Gateway** HTTP API and realtime
socket. Pegasus is a fleet telematics platform by Digital Communications
Technologies (DCT); some tracking providers run white-label Pegasus deployments,
and pym2m is named after one of them.

Every endpoint, parameter, field name and behaviour implemented here was taken from
**publicly available** Pegasus developer material:

- Public developer documentation at `pegasus.digitalcomtech.com/docs/*`
  (authentication, API overview and rate limits, live communications, segments and
  trips, counters, geofences, forwarders, reverse geocoding, master fields list).
- The public Pegasus Postman API reference linked from that documentation.

No vendor document distributed under confidentiality terms was copied, quoted or
paraphrased into this repository. Where pym2m relies on something the public
material does not state, the code says so and treats it as unverified.

Test fixtures are synthetic. Identifiers, coordinates, tokens and names are
invented. No customer data, credentials or private hostnames are included.

## Verification status

| Capability | Basis | Offline tests | Verified against a live account |
|---|---|---|---|
| Login / scoped token use (`Authenticate` header) | public docs | yes | **no** |
| API root / `live_url` | public docs | yes | **no** |
| Vehicles, devices, latest location (`select`) | public docs | yes | **no** |
| Rawdata history | public docs | yes | **no** |
| Trips (`/trips`) | public docs (response shape varies by `fields`) | yes | **no** |
| Counters | public docs | yes | **no** |
| Geofences, triggers, alerts (read) | public docs | yes | **no** |
| Reverse geocoding (`/reversegeo`) | public docs | yes | **no** |
| Realtime socket (Socket.IO 2.x / Engine.IO 3) | public docs + Socket.IO 2.x wire protocol | yes, including a real `socket.io@2.5.0` server | **no** |
| Forwarder payload parsing (`json_fwd`) | public docs | yes | **no** |
| Remote commands / any write | intentionally not implemented | n/a | n/a |

## Publication status

Public since 2026-10-13, on the owner's authorization. Before the repository was
made public, its full git history was scanned: it contains no vendor documents,
no confidential text, no credentials and no customer data.
The Apache-2.0 license covers this code only. It grants nothing over any vendor
documentation, trademarks or services.
