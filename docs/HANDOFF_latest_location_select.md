# Latest-location projection correction

The repeated device selector silently omitted latest location in live responses.
`client.py` now combines the nested projection; `params.py` accepts the documented
space/plus nested fields using full-string validation. Version: 0.2.3 (not released).
Synthetic tests emulate last-resource-wins behavior and reject malformed selectors.

Validation: 140 tests passed, two optional Socket.IO server tests skipped; Ruff lint
and formatting passed. A bounded authorized live check returned usable latest fixes
for both selected vehicles with the correction. No live data is committed.

Risk: callers using undocumented selector punctuation will now fail validation.
No endpoint, authentication, transport, or mutating capability was added.
Human merge/release and downstream deployment remain required.
