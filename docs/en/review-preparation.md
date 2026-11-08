# Review queue preparation

Navigation and Back also work in a direct LAN test UI: history initialization does not require `crypto.randomUUID`. Production retains HTTPS and its existing authentication and mutation protections.

Open **Collection → Needs review**. **Open review queue** on the Overview and the guide's general review action open this same preparation panel. Select a track to enter its editor; the separate last-prepared-folder action retains its own filter. Categories are exclusive: file problems take priority, then composite recordings, then proposals. Counts cover the entire collection; search further narrows the list. These are tracks requiring a decision, not collection completion percentages.

1. **Preparation** - additional evidence can be prepared or a task is running. **Prepare available** queues at most 200 tracks of each kind per launch. Alignment uses the configured ASR; predominantly Russian reference words receive a `ru` hint. This does not install or validate a new ASR model.
2. **Choose a candidate** - compare the proposal with the file. Unchanged and stale proposals remain in history but do not alone create review work.
3. **Listen** - evidence is insufficient, preparation has finished, or the allowance is exhausted. Alignment initially displays missing timestamps and low-confidence lines; all other lines can be expanded. A match does not accept words or timing.
4. **File problem** - resolve availability, corruption or revision conflicts.

Progress and cancellation are in **Processing**. Track messages distinguish cancellation, failure and completion. Preparation is not repeated for the same plan after reload, double click or restart. A manual retry remains available in the track editor, subject to the same provider allowance. A new proposal, file revision or decision changes the plan; stale requests are rejected.

A machine-generated mix suspicion is rechecked using up to three separated fragments within the existing shared allowance. Clearing it requires a 30-600 second recording, three distinct audio samples, identical recording ID and ISRC, and offsets spanning at least half the recording. Partial responses, different songs, missing identifiers, unknown outcomes, long recordings and manual classifications keep review open. Clearing a machine flag does not accept tags, edition or lyrics.

Album batch review is available only from a current release review with a verified member set. It opens the existing comparison of shared tags, cover and numbering, preserving exclusions and provenance. Similar folders or artists do not establish an edition.

## Contract and verification

`GET /api/review/plan` returns categories, bounded batches and verified album groups. `POST /api/jobs` with `review_tokens` accepts only `recognize` and `align`, checking the current file, proposal, classification, source policy and recovery state. The plan receipt and job are committed atomically in SQLite. Existing cancellation, authorization, provenance and quotas remain mandatory. Writing tags, applying timestamps and publication use their separate existing actions.

No schema migration is required. Tracing is unchanged. Verification covers identical populations and category totals, retained history, duplicate delivery, cancellation, stale plans, source restrictions, exhausted quotas, unanimous/disputed fragments, album freshness, filter restoration and mobile layout. Actual musical evidence determines how many flags can be resolved; queued task counts do not prove success.

The repository-standard CI bootstrap has a bounded curl fallback if the Node downloader cannot fetch Gitleaks. Both transports use the same official 8.30.1 archive and mandatory SHA-256 verification; a failed download or checksum still blocks the check. The toolkit and secret-scanning rules are unchanged.
