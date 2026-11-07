# phonoteka

The catalog and track editor explain each missing tag, cover and synchronized lyric field. Reasons distinguish an unattempted search, stale results, temporary provider failures and candidates awaiting review or writing. Matching MusicBrainz editions are selectable album candidates. [Continue enrichment](docs/en/enrichment-gaps.md).

Full reports read metadata sequentially without retaining the collection's cached artwork and drafts: [report memory](docs/en/report-memory.md).

Local tag encoding repairs retain their file origin: [rules and limits](docs/en/local-tag-encoding.md).

Ready-file snapshots can use **Artist / Album / Track** folders with portable names and collision protection. The manifest retains the original Ready paths. Work files, accepted revisions and provider restrictions remain unchanged. See [catalog export](docs/en/collection-workflow.md#catalog-export).

After CUE extraction or audio recovery, collection progress counts the prepared files. Retained inputs are listed separately, and pending-search counts use the same expiry rules as launching a job: [prepared files and sources](docs/en/prepared-sources.md).

CI checks installed media tools and uses the official Ubuntu HTTPS repositories when installation is needed. See [installation and timeout diagnosis](docs/en/ci-codecs.md); package signatures and all media tests remain required.

ASR uses decoded sample duration for raw MP3 files with inaccurate header estimates. The result retains the reported estimate; the 30-minute limit is checked before contacting PC2. [Details and limits](docs/en/recovery-alignment.md).

After a worker crash, unfinished read jobs pause while retaining completed results and provider budgets. ASR checks the actual reference words separately from tags and artwork: [recovery and alignment](docs/en/recovery-alignment.md).

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

Record review offers one next action; the activity log provides search, a problems filter and scroll pause. Settings are above the account in the left menu. See the [collection workflow](docs/en/collection-workflow.md).

Sign-in is controlled by AUTH_ENABLED (enabled by default). [Configuration and restoring authentication](docs/en/authentication.md).

Guided setup prepares a selected directory in the mounted music folder for review and refreshes expired search results. Accepted revisions can be published through the plan and downloaded with the playlist in one ZIP. [Folder-to-collection workflow](docs/en/collection-workflow.md)

ASR coverage counts lyric content rather than blank LRC markers. A current MP3 decoding failure can be processed into a separate verified PCM copy while preserving the original; this does not restore missing audio.

Set PROVIDER_DAILY_LIMIT=0 to disable Phonoteka's local daily request quota while preserving provider pacing and cooldowns. [Quota, Retry-After and timeout checks](docs/en/provider-quotas.md).

Large snapshots use the same 32 MiB manifest limit for reading and pre-copy validation. Existing snapshots above the former 10 MiB limit become accessible without re-exporting their audio. [Size limit and verification](docs/en/export-manifest-size.md).
