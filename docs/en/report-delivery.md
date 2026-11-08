# Large collection reports

JSON and CSV reports preserve the same fields and single database read snapshot. The server finishes writing an unlinked temporary report before sending HTTP headers, then sends it in bounded chunks. A generation or temporary-storage failure cannot turn a partial document into a successful download. The file closes on completion, network failure or cancellation.

This avoids retaining a complete Python JSON string alongside its UTF-8 HTTP body. Unicode outside the basic multilingual plane can otherwise expand the entire string to four bytes per character. Report construction still retains the projected collection rows; this change is not a claim of constant memory for unlimited collections. Temporary storage must fit the report, and storage on tmpfs still counts against the container memory limit.

Acceptance compares JSON and CSV bytes with the existing report contract, including Unicode, CSV formula escaping, source policy and publication state. Tests cover generation failure and response cancellation. Audio snapshots, leases, checksums, provider policies and tracing are unchanged; existing exports do not need to be rebuilt.

## Ready snapshots during downloads

`GET /api/exports/latest` also serializes its complete response into an unlinked temporary file before sending headers. The manifest, provenance, receipts, counts and stale-file indicators keep their existing JSON contract. FastAPI no longer recursively copies this large response and creates whole Unicode and UTF-8 bodies while audio downloads are active.

Individual downloads keep one snapshot index per application instance, containing only paths, hashes and usage provenance. Concurrent requests share the initial read. The server safely opens and size-checks the manifest on every request; device, inode, size, modification time and change time invalidate the index when a snapshot changes. Duplicate paths are rejected. Current source permissions and the full audio hash are checked on every download, including cache hits. A deleted manifest cannot be served from the index. Archive streaming and download leases retain their existing checks.

Acceptance includes simultaneous manifest consumers, Unicode response equivalence, concurrent index loads, changed audio, revoked source permissions and a same-size manifest edit with its modification time restored. Memory measurements remain bounded-fixture evidence, followed by real collection delivery acceptance; they do not establish unlimited collection capacity.
