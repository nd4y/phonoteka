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
