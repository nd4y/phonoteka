# phonoteka

**Needs review** offers bulk filling of independently confirmed tags and artwork, separately
from lyric acceptance. Preview changes and remaining reasons before submitting; disputed fields
stay pending. [Rules and workflow](docs/en/confirmed-fields.md).

Processing counters report completed attempts and errors, without claiming data approval.
Current partial ASR results explain the missing complete lyrics and open lyric review.
[Missing-field explanations](docs/en/enrichment-gaps.md).

ASR cancellation removes scratch files after the owned process tree is reaped.
Transient Windows log locks get bounded cleanup retries; permanent errors remain visible.

An optional pinned PC2 GigaAM profile recognizes Russian words and their real times;
Whisper retains the final alignment gates. Unknown confidence and drafts remain
explicit. [Russian ASR contract and acceptance](docs/en/pc2-russian-asr.md).

Status and job activity recover after a stalled server read. Failed updates are shown separately from task results and clear after recovery. [Connection recovery](docs/en/ui-connection-recovery.md).

Skipping a record shows a next-record transition, without claiming that the file is being written. [Review feedback](docs/en/review-queue-feedback.md).

A saved proposal shows its cover beside the current file's cover before writing the audio. [Record review](docs/en/review-preparation.md).

**Needs review** groups tracks by the next action: prepare data, choose a candidate, listen, or fix a file. Open the queue from the Overview, guide or collection menu. **Prepare available** queues bounded recording rechecks and lyric alignment drafts; disputed results remain pending. The summary, category counters and list use the same rules. [Review workflow](docs/en/review-preparation.md).

**Music gaps** narrows the collection to individual recordings with missing tags, cover art or timed lyrics. The detailed report opens these gaps for each field. [Find gaps in music](docs/en/enrichment-gaps.md#finding-gaps-in-music).

When tracks on a page share a title and artist, the list shows each copy's relative path.

Write confirmed artwork separately and keep the lyrics draft for review.
Collection actions can retry search and recognition within the selected music category.

After an internal search or recognition failure, **Continue remaining files** in Processing continues the same task from files whose processing never started.
Processing keeps recently updated results visible, including jobs that started much earlier.
Tasks that can continue are listed before regular completed history.
The missing-cover list explains whether the recording, album or exact edition still needs confirmation, or whether the cover search can run now. The missing-lyrics list shows its own reason in each row.
The collection list refreshes when the query, filter or page changes and when processing finishes.

If the album is known, refine its release and cover search by title in the track sources.

Album batch previews show which earlier proposal fields will remain alongside the selected edits. Check these fields in the track editor before writing the file.
After selecting shared artwork, wait for its file name next to the thumbnail before previewing the batch.
In the track editor, the artwork slot button opens the list of embedded images.
After choosing a slot, apply the artwork preview to the draft before saving. Choosing a slot alone leaves the file unchanged.

To work on a few files from a large album, exclude all files and include only the ones you need. Check the selected count before previewing.
Search for an album or artist in the detailed report to find groups beyond the initial list.
Release choices show the available date, country and short ID so you can distinguish editions with the same title.

The Ready panel shows when Phonoteka is checking files before plan confirmation and submitting the job after confirmation. After switching tracks, wait for the receipt for the new track revision.

The accepted revisions waiting for publication filter lists files that still need separate delivery. Retained CUE images and damaged inputs stay visible in the full plan. A source with no Ready publication drops out of the waiting list once all its verified outputs have current Ready revisions.
After confirmation, the panel shows the publication job state and the number of files saved.
Previous rows cannot be opened while a search or filter is updating. The proposal confirmation names the file path.

Temporary SQLite contention while updating the export counter no longer aborts a verified snapshot. File checks and the final commit remain mandatory. [Export progress and contention](docs/en/export-contention.md).

The catalog and track editor explain each missing tag, cover and synchronized lyric field. Reasons distinguish an unattempted search, stale results, temporary provider failures and candidates awaiting review or writing. A failed new search is shown separately from its retained earlier results. Matching MusicBrainz editions are selectable album candidates. After an interrupted task is recovered, its remaining files can be cancelled while preserving completed changes. [Continue enrichment](docs/en/enrichment-gaps.md).

A saved timed draft opens the Lyrics tab for a check against the vocals. The status summary sorts only format names, so larger track metadata does not enlarge that sort.

Search and ASR started from a track card refresh that card after the job finishes. The refresh waits while you edit or choose sources.
If writing a file outlasts the initial wait, the open card shows the new revision when the job finishes.

Line review applies to a specific file revision and lyric version. After a tag or cover update, earlier decisions remain in history. [Review the current version](docs/en/enrichment-gaps.md).
If the Ready plan times out, use Refresh Ready plan to retry from the same view.

Source details explain when the audio recognition allowance is exhausted. Previously found candidates from other sources remain available for comparison.

After configuring AcoustID, an unfinished search can continue without resending conclusively checked fragments to Shazam. Historical recording IDs are retained as evidence, while expired tags are not reused. The existing attempt allowance and current-file evidence requirements remain in force.

Full reports read metadata sequentially without retaining the collection's cached artwork and drafts: [report memory](docs/en/report-memory.md).
Review counts also read records one at a time. The interface skips an automatic status poll while its previous poll is still running.

Local tag encoding repairs retain their file origin: [rules and limits](docs/en/local-tag-encoding.md).

Ready-file snapshots can use **Artist / Album / Track** folders with portable names and collision protection. The manifest retains the original Ready paths. Work files, accepted revisions and provider restrictions remain unchanged. See [catalog export](docs/en/collection-workflow.md#catalog-export).

After CUE extraction or audio recovery, collection progress counts the prepared files. Retained inputs are listed separately, and pending-search counts use the same expiry rules as launching a job: [prepared files and sources](docs/en/prepared-sources.md).

CI checks installed media tools and uses the official Ubuntu HTTPS repositories when installation is needed. See [installation and timeout diagnosis](docs/en/ci-codecs.md); package signatures and all media tests remain required.

ASR uses decoded sample duration for raw MP3 files with inaccurate header estimates. The result retains the reported estimate; the 30-minute limit is checked before contacting PC2. [Details and limits](docs/en/recovery-alignment.md).

Choose the recording language in Sources before transcribing audio or aligning lyrics when automatic detection picks the wrong language.

The LRCLIB comparison shows when it removes an empty marker just after the audio ends. It preserves the words and their timestamps for review.

After a worker crash, unfinished read jobs pause while retaining completed results and provider budgets. ASR checks the actual reference words separately from tags and artwork: [recovery and alignment](docs/en/recovery-alignment.md).

If the catalog is briefly busy after ASR finishes, the worker retries storing the result without sending another request to the engine.

Recovered Ready publications retain a receipt that the Music relay can verify against committed history.
The optional delivery process ships as `python -m phonoteka.music_sync`. Persistent directory identity tolerates device renumbering while a replaced root or external file edit blocks delivery. See [Music setup and recovery](docs/en/music-delivery.md).
The existing Drive notifier supports both ledger formats and must be upgraded with delivery before identity migration.

Provider cache maintenance processes short catalog batches and skips unchanged provenance records so that editing and review can continue between batches.

Recovery preserves the original audio file; invalid embedded artwork remains in that source and is explicitly recorded as omitted from the derivative.

Scanning counts only validated JPEG/PNG artwork. HTML stored in an image frame and unsupported image formats produce a warning, do not increase the cover count, and do not prevent replacement lookup. Inspection preserves the original bytes.

Owners can configure AcoustID in the UI: save, replace or remove an Application API key, enable recognition and test the connection. Chromaprint lookup supplements inconclusive Shazam results and produces review candidates. See [setup, transmitted data and limits](docs/en/acoustid.md).

Recognition reports count each file outcome once; old AcoustID key-required markers do not become recognition errors after upgrading.

Development and contribution entry point. Product capabilities, installation and current limitations are described in the [existing product README](README.md). Full translation is tracked as unverified; this page does not claim translation parity yet.

## Development standard

This repository adopts version 0.4.0 of the shared development standard. Read the [English guide](docs/en/development-standard.md) or [Russian guide](docs/ru/development-standard.md). The local toolkit checks staged secrets, changed-file formatting and bilingual documentation before merge. Existing product CI remains in place. Adoption is in progress; product acceptance and legacy-document translation are recorded separately in .repo-policy.json.

## Collection workflow

Start with Overview for the next action, review proposals, check processing outcomes, then transfer ready files. See the [interface workflow](docs/en/collection-workflow.md).

The saved-proposal count is narrower than the full review queue, which also includes errors and conflicts. An empty Ready files section explains how to publish reviewed revisions; an empty search suggests refining the query.

The selected row indicates that its record is opening. Changing the search keeps a late response from opening the previous record over the new results.

If the list takes more than 25 seconds to load, use **Retry** beside the search. The query and filters are preserved.

Record review offers one next action; the activity log provides search, a problems filter and scroll pause. Settings are above the account in the left menu. See the [collection workflow](docs/en/collection-workflow.md).

Sign-in is controlled by AUTH_ENABLED (enabled by default). [Configuration and restoring authentication](docs/en/authentication.md).
With sign-in disabled, an open tab refreshes a stale write token after a server restart.

Guided setup prepares a selected directory in the mounted music folder for review and refreshes expired search results. Accepted revisions can be published through the plan and downloaded with the playlist in one ZIP. [Folder-to-collection workflow](docs/en/collection-workflow.md)

ASR coverage counts lyric content rather than blank LRC markers. A current MP3 decoding failure can be processed into a separate verified PCM copy while preserving the original; this does not restore missing audio.

Set PROVIDER_DAILY_LIMIT=0 to disable Phonoteka's local daily request quota while preserving provider pacing and cooldowns. [Quota, Retry-After and timeout checks](docs/en/provider-quotas.md).

Large snapshots use the same 32 MiB manifest limit for reading and pre-copy validation. Existing snapshots above the former 10 MiB limit become accessible without re-exporting their audio. [Size limit and verification](docs/en/export-manifest-size.md).

Collection reports and Ready snapshot responses are completed in temporary storage and downloaded in chunks, avoiding duplicate full JSON bodies in memory. Individual audio downloads share one validated snapshot index while rechecking current source permissions and file hashes. [Memory limits, cleanup and verification](docs/en/report-delivery.md).

Sources groups candidates by song tags, releases, lyrics, and artwork. Compare changes shows the resolved result per field without saving; a separate action saves the reviewed selection as a proposal. [Choosing source data](docs/en/collection-workflow.md#choosing-source-data).
Other requests continue while a comparison or save waits for catalog access.
If the catalog is temporarily busy after writing a Ready file, the worker retries saving the verified publication receipt.
Clear the checkbox for any field you want to keep as it is.

Confirmed-field batches show per-record gap reasons and the fields actually written in processing results.

Confirmed-field filling can prepare missing evidence in a bounded batch of stability checks and source searches without writing audio files. Refresh the plan afterwards; disputed candidates remain pending. [Workflow and limits](docs/en/confirmed-fields.md#preparing-missing-evidence).
