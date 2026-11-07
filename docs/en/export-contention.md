# Export progress under database contention

`EXPORT-PROGRESS-BUSY-01`: the copied-file counter is advisory. A temporary SQLite BUSY or LOCKED error when saving this counter leaves the previous sample visible and allows copying to continue. The rolled-back transaction is closed, and the next sample tries a fresh transaction. Progress writes remain rate limited.

This exception applies only to the progress update. Authorization, source and destination SHA checks, provenance and revision checks, snapshot commit, and job completion retain their existing failure behavior. Other database errors still fail the export. A failed attempt does not replace the previous immutable snapshot. The JSON log includes the numeric SQLite error code without its message, SQL query or private paths. Tracing configuration is unchanged.

Acceptance: `tests/test_export_contention.py` holds a real SQLite writer lock at the initial, middle and final progress callbacks, then verifies the generated snapshot bytes and single completed job item. A non-contention database failure must still fail and retain the previous snapshot. `tests/test_observability.py` checks that the numeric code does not expose query details. An installed collection export remains a separate release check.
