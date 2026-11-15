# Incremental scanning

The primary collection action is **Scan changes**. It still discovers paths in
the configured Work directory, but fully reads only new, changed, failed or
unfinished inputs. New inputs still require two independently completed reads
and the existing stability grace period. A stat observation cannot replace the
second pass.

A previous successful inventory is reused only when its size, modification time
and saved revision agree. A changed ctime, device or inode also requires a read.
For legacy records without an inode snapshot, ancestor directories must not have
changed since the full read. Planned skips are checked again before completion;
an edit during other work returns that file to inspection. This is change
detection, not a fresh content proof: skipped records retain their existing
SHA-256 verification date and independent stability evidence. Audio writes keep
their full SHA compare-and-swap checks.

Directory names and stat metadata must still be read to discover new and absent
paths. Existing extensionless discovery reads a short header. CUE documents are
revalidated with their audio references, since an unchanged sheet may reference
a removed or replaced file. Failed inputs remain visible and are inspected on
the next run.

**Collection actions → Full scan** forces a complete read and SHA-256 check,
including after a filesystem restore or when its metadata is untrusted. Work
mount reconciliation remains a separate administrative operation.

Processing displays discovered paths, records reused without another full read
and actual inspection attempts separately. Reused records do not inflate the
attempt counter or create one journal entry per unchanged file. A no-work scan
finishes with an explicit no-changes message.

Scanning does not accept tags or lyrics, publish audio or call music providers.
Cancellation and enumeration errors do not mint another stability proof or mark
unobserved paths missing. Previously accepted jobs without the new parameter
retain full scanning. The bounded two preparation passes keep their frozen
input sets.

The API freezes `scan_mode: incremental` for new ordinary jobs by default;
`scan_mode: full` forces full reads. Other job kinds and mount reconciliation
reject this parameter. Code, build-01 tests, GitHub checks, deployment and the
installed UI have separate acceptance evidence in the task record.
