# Large collection reports

JSON and CSV reports preserve the same fields and single database read snapshot. The server finishes writing an unlinked temporary report before sending HTTP headers, then sends it in bounded chunks. A generation or temporary-storage failure cannot turn a partial document into a successful download. The file closes on completion, network failure or cancellation.

This avoids retaining a complete Python JSON string alongside its UTF-8 HTTP body. Unicode outside the basic multilingual plane can otherwise expand the entire string to four bytes per character. Report construction still retains the projected collection rows; this change is not a claim of constant memory for unlimited collections. Temporary storage must fit the report, and storage on tmpfs still counts against the container memory limit.

Acceptance compares JSON and CSV bytes with the existing report contract, including Unicode, CSV formula escaping, source policy and publication state. Tests cover generation failure and response cancellation. Audio snapshots, leases, checksums, provider policies and tracing are unchanged; existing exports do not need to be rebuilt.
