# Rawdata synchronous request limit

A controlled live verification found that the provider rejects a synchronous rawdata request above 25 vehicles with HTTP 400, reason non_async_many_vehicles, limit 25. Splitting the same bounded window into requests of 25 and 3 succeeded. No customer identities, coordinates or credentials are included here.

client.py now splits accepted calls into requests of at most 25 vehicles, preserving the public 50-entity limit, fields, source and globally sorted results. Unsupported later responses raise without returning partial history. Version is 0.2.4; changelog and boundary/partial-result regression tests updated.

Validation: full pytest 144 passed, 2 skipped; ruff check and format check passed; strict mypy passed (17 source files). The new batching behavior has not been deployed or tested as an automatic worker recovery. Human merge and downstream pin/deployment precede that repeat test. Larger time windows may still return asynchronous jobs, which remain explicitly unsupported.
