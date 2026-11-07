# From a folder to organized music

## Catalog export

In Ready files, **New snapshot: Artist / Album / Track folders** controls the next snapshot. It is enabled initially; deselect it to keep Ready paths. Existing snapshots retain their own layout until a new one succeeds. The API accepts `params.layout: "artist_album"` for `export_ready`; omitted values keep the compatible `source` layout.

Catalog folders use album artist (or artist), album, disc/track numbers when present, and title. Missing artist or album produces an `Unknown Artist` or `Unknown Album` folder; missing title uses the source filename. These labels do not fill tags or claim recognition. Case, Unicode, reserved characters and long names are normalized for portable paths. Collisions receive stable suffixes instead of overwriting or merging unrelated folders. The manifest records `source_path` alongside each new path and checksum.

Only accepted, current Ready revisions enter the snapshot. LRC files stay beside their audio; registered cover sidecars use a matching `.cover.jpg` or `.cover.png` name. Shared cover content may be copied for each track while retaining all source permissions and provenance. The ZIP and playlist use the new names. Work, Ready and original audio are not renamed. Profile exceptions remain in the manifest; folder organization is not a claim of complete metadata, listening acceptance or device verification.

## Review and delivery

Preparation and the pending-files search include expired provider matches whose content is no longer available. A historical successful search does not make those files ready for review. Current usable results and completed searches without matches are reused. Provider permissions still apply to each new request.

The four main sections are **Overview**, **Collection**, **Processing**, and **Ready files**. Settings are in the left menu above the account button. Help opens guided setup. Previously saved navigation, filters, and scroll positions remain available after refresh.

1. **Overview** suggests the next action. Its result count refers to current audio revisions already published into the output collection. CUE documents are separate. Empty or unknown totals never imply 100% completion. Proposal, error, and missing-data counts may overlap.
2. Search or use quick filters in **Collection**. Bulk operations are disclosed separately. Opening a record does not replace the playing audio. **Listen to this record** explicitly selects playback; player previous/next controls change playback independently of the editor.
3. Compare each field as **In file / To be written**. A current proposal initially shows changed fields; all fields can be revealed. Keeping the original value updates the draft. **Save draft** saves a proposal without writing the audio file.
4. **Write and next** queues a saved proposal and opens the next record only after a confirmed successful file write. Errors, cancellation, and unknown outcomes keep the editor open. A pending task can be checked in Processing or checked again without resubmission. Unsaved edits must be saved first; **Skip and next** does not change the file. Check the outcome in **Processing**. Page boundaries load the next catalog page and the end of the list is explicit.
5. **Sources** contains search, YouTube, and optional fingerprint, ASR, and loudness tools. No match does not prove an absence of vocals or prevent manual editing. Lyrics acceptance, field locks, publication, and recovery remain in separate disclosures below the editor.
6. **Processing** shows state, percentage of processed attempts, ETA, and the current action. A completed task does not establish metadata correctness. Event logs expand on demand; live updates, job controls, and trace links remain available.
7. Open the publication plan under **Ready files**. Blocked entries show reasons and a link to the record. Preview eligible revisions on the current page and confirm the selection. Once published, prepare a snapshot and download the collection ZIP. Extract it on the phone and add the folder in Poweramp. Copy receipts and playback verification are separate actions.

At the start of guided setup, select a directory inside the installed music mount and the desired quality profile. The one-time preparation scans, recognizes and searches for metadata. Disabled providers are skipped. The server retains progress across reloads. Open the resulting folder queue to review the files. Work-file writes, profile acceptance and publication each require an explicit action.

On narrow screens, each field retains its before/after pair, and write controls sit above the compact player. Back dismisses an open disclosure or the Sources tab before returning through application actions. Unsaved-change protection remains in place.

The guide labels its input/output path example as illustrative, not a real rename result. This UI release does not alter available sources, formats, quality profiles, provider permissions, or the tracing architecture.

The **Saved proposals** counter counts current proposals only. Its shortcut opens the entire review queue, including records with errors or conflicts; the queue can therefore contain more records than the counter. In **Ready files**, an empty collection directs you to review records and publish them through the existing plan. An empty search instead suggests changing the query; neither state asks you to rescan the working collection.

## Record review and activity log

Settings are in the left menu above the account, or in the bottom navigation on phones. Interface labels are neutral; access control and operation restrictions are unchanged.

Review shows three steps: find data, review, write. One main button offers the next action: find/select data, save an edited draft, fix lyrics, or write a saved proposal and continue. Saving a draft does not modify audio. An unconfirmed recording version, conflict or validation error can still block writing. Use audio recognition for an unknown song and catalogue search when artist and title are known. Additional operations remain available in expandable sections.

An expanded job shows the latest 200 loaded events. Search matches message, filename, code and provider; the problems filter includes failures and authorization waits. Pausing scrolling freezes the visible window for reading while processing and event reception continue. Jumping to latest resumes the live display. Search and filter changes pause scrolling. These controls do not search the complete server history.

The Review lyrics action focuses and reveals the line editor. Normal navigation and reload retain their existing position restoration; returning from the initial source search takes one Back action.

## Decoder failures and alignment

Strict verification checks both FFmpeg's exit code and error-level diagnostics. A decoding error cannot pass verification just because FFmpeg exits with code 0; ordinary warnings remain separate diagnostics.

A current full-audit decoding failure for a single MP3 stream offers a readable copy. The conversion retains decoded audio as FLAC or PCM WAV, compares decoded PCM SHA-256 and fully audits the output. It preserves the source and records the recovery origin. Missing or damaged original samples are not restored; listening remains necessary. Unreadable headers and stale audit revisions do not qualify.

ASR coverage and timing comparisons count lyric content only. Blank LRC markers retain their original times in diagnostics but receive no invented anchors. A complete candidate includes the content rows only and is offered only when every such row aligns. Partial results retain original times separately and produce no complete LRC. Human listening and Poweramp acceptance remain separate.
