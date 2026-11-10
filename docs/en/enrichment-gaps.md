# Why a field is missing

The catalog shows a brief reason below the track name. Open the track to see “What remains to fill”: a separate explanation for each missing title, artist, album, cover and synchronized lyric field. Each row includes a next step, provider and analysis update time when known. This timestamp is not the time of each individual provider request.

The page indicator shows a loading state and disables pagination until the requested page arrives.

In Sources, open the album search refinement when the track title, artist and album are known and the file lacks an album or cover. The search finds MusicBrainz releases with the given album title and their covers. Earlier lyric results remain available. Review the edition and select the fields before writing.

- An unattempted or stale search offers a search against the current file revision.
- Artwork gaps explain whether the title or artist, album, or exact release is still needed. When a suitable release is known, the catalog shows whether its artwork has been requested.
- Candidates require review; a saved proposal still needs writing to the file. A draft for one field does not explain other missing fields.
- Temporary provider errors or throttling mean the search is incomplete. Retries respect provider cooldowns; IP rotation and quota resets are not used.
- If a new search or recognition attempt for the current file fails, the track shows that failure instead of an earlier gap explanation. Previous analysis and saved proposals remain available. The timestamp is the job update time, not an individual request time; processing contains error details. A successfully refreshed analysis supersedes the old failure explanation. Recognition is never retried automatically and its attempt allowance is not reset.
- Expired results need refreshing, whereas denied storage or embedding requires checking the provider usage settings. AcoustID that is configured now is not shown as missing a key solely because of an earlier search.
- Plain lyrics can be reviewed and aligned with the audio. A provider's instrumental claim requires confirmation and does not automatically remove lyrics from the task.
- No result refers only to the last query; it does not prove that enrichment is impossible.

Actions are disabled while the editor has unsaved changes. Field coverage does not establish correct identification, timing against vocals or compatibility with a particular player.

When an ASR result is retained, the missing-lyrics explanation includes the fraction of lines aligned in that attempt. A partial result is not a complete LRC; even 100% coverage requires listening against vocals. This is neither queue progress nor confirmation that an earlier reference text is still current.

When a source policy blocks saving manual changes, choose **Restore saved values and open sources**. This discards unsaved changes. Select an available source and compare the fields before saving.

## Finding gaps in music

When uploading a cover, select the embedded slot and apply the artwork preview to the draft. A slot selection by itself does not create a change to write.

Choose **Music gaps** in the collection or select a specific field in the **Gaps in individual music recordings** group. These filters skip possible mixes, CUE sheets and album images, and entries explicitly tagged as speech, audiobooks or podcasts. Entries with no genre remain visible for review. The filter sets the list and playback order. In **Collection actions**, retrying search or recognition covers all eligible records in the selected music category, including those outside the current text search. Review the results before writing them.

If a saved proposal contains artwork and lyrics, choose **Write cover only**. The lyrics draft stays available for review when the audio and native lyric slots are unchanged.

The detailed report has separate entry points for core tags, artwork and timed lyrics. They open gaps in individual recordings. The cards count the entire audio catalog, so the number of results after opening a filter can differ.
The artwork and timed-lyrics filters show the reason for that field in each row. The track editor still lists the other gaps.

Cover Art Archive choices show the MusicBrainz edition information when available, or the release ID. A match with the file's embedded release ID is shown separately. Choosing and saving a cover still requires review.

## Search coverage

When LRCLIB's exact result contains only plain text, the search also checks other variants. If that extra request fails, the plain result is preserved and the search remains incomplete. Cover lookup checks up to three matching album editions, or an explicit MusicBrainz release ID embedded in the file. Artwork remains subject to review; multiple editions no longer suppress the entire lookup.

Catalog explanations use a compact SQL projection without cached artwork, song lyrics or transcripts. Reading reasons does not enqueue processing or modify the collection.

Releases of matching MusicBrainz recordings appear as separate album candidates. Selecting an edition proposes its album name, MusicBrainz release ID and available date while preserving the song title and artist. When the album is missing, cover lookup runs only if all returned editions share one album name; different album names require selection. Nothing is applied automatically. The list is bounded to eight edition candidates.

Fingerprint recognition preserves an existing album in the search query when both embedded title and artist exactly match the recognized identity. Conflicting or missing identity tags use the fingerprint result instead.

An exhausted fingerprint-attempt allowance is distinct from a temporary provider cooldown; a new queue does not reset it. An unknown outcome of a sent request is not a confirmed no-match. Configuring a key does not offer an ineffective recognition retry when the same audio identity has already exhausted its allowance.

If an earlier Shazam search conclusively found no match in every required fragment while AcoustID was unavailable because its connection was disabled or lacked a key, recognition can continue directly with the newly configured AcoustID. The file revision, fragment offsets, extractor profile and sample digests must still match the recorded evidence. Positive, incomplete, conflicting and unknown results are never treated as negatives. The new lookup retains cancellation, provider caching and the same durable attempt allowance; it does not reset or grant calls.

Consistent historical recording IDs can also qualify when their matching verdict is preserved and the old provider tags are no longer available. They remain historical positive evidence. Only the fresh AcoustID result can supply a new fingerprint-based metadata query; expired titles, artists or releases are not restored.

## Interrupted publication

An interrupted task on the processing screen offers **Cancel remaining files**. This preserves completed changes and closes the remaining work. It neither rolls back nor automatically retries files: after reviewing the results, select only unfinished records for another operation.

The server permits cancellation only after operation journals have been reconciled and a fresh running-worker heartbeat confirms recovery has finished. Until then, the UI asks you to wait. Cancellation also remains blocked while a restored database requires acknowledgement. Completed operations and their history are retained.

For parallel-processing failures, JSON logs include up to eight nested exception types and abbreviated stacks. Exception messages, audio paths, requests and secrets are not added to the log. OpenTelemetry settings remain unchanged.

## Continue after a search failure

After an internal search or recognition failure with unstarted files, **Continue remaining files** uses the original task and scope. Completed and failed results remain in its history. An admitted file whose outcome was lost is marked for review, since a provider request may have been sent already. Only files with no recorded start are admitted. The original authorization and provider budgets still apply.

The worker retries the same local per-file checkpoint up to three times on SQLite contention. No provider request is repeated by this retry. The progress summary groups review cases and errors under **require attention**. File details show the individual reason.
