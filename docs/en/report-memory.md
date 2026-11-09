# Collection report memory

JSON and CSV reports read current track metadata one record at a time. Analysis
queries select only the revision, status and provider status list needed by the
report. Cached artwork and ASR drafts are not accumulated across the collection.

The report still uses one database read snapshot. Stale analyses, source usage
restrictions, prepared CUE/audio outputs and the latest per-file outcomes keep
their existing meaning. Output contains status codes, not private provider
messages or unaccepted transcription text.

The regression fixture contains 72 synthetic tracks with 128 KiB of unreported
metadata and 128 KiB of draft data per track. JSON and CSV exports must stay below
12 MiB of traced Python allocations while preserving the report rows and source
states. This isolates report allocation growth; it is not a RAM guarantee for the
whole service or a substitute for testing the installed collection.

Review counts and categories read the saved lyrics and proposal for one track at
a time. The regression uses 72 synthetic tracks with 96 KiB text fields and checks
unchanged counts with peak Python allocations below 4 MiB. A tab skips automatic
status polls while its previous poll is pending. User-triggered refreshes remain
available.
