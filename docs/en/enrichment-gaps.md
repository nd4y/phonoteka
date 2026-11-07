# Why a field is missing

The catalog shows a brief reason below the track name. Open the track to see “What remains to fill”: a separate explanation for each missing title, artist, album, cover and synchronized lyric field. Each row includes a next step, provider and analysis update time when known. This timestamp is not the time of each individual provider request.

- An unattempted or stale search offers a search against the current file revision.
- An unattempted separate cover search does not mean artwork is unavailable online. Check the album and release candidates first.
- Candidates require review; a saved proposal still needs writing to the file. A draft for one field does not explain other missing fields.
- Temporary provider errors or throttling mean the search is incomplete. Retries respect provider cooldowns; IP rotation and quota resets are not used.
- If a new search or recognition attempt for the current file fails, the track shows that failure instead of an earlier gap explanation. Previous analysis and saved proposals remain available. The timestamp is the job update time, not an individual request time; processing contains error details. A successfully refreshed analysis supersedes the old failure explanation. Recognition is never retried automatically and its attempt allowance is not reset.
- Expired results need refreshing, whereas denied storage or embedding requires checking the provider usage settings. AcoustID that is configured now is not shown as missing a key solely because of an earlier search.
- Plain lyrics can be reviewed and aligned with the audio. A provider's instrumental claim requires confirmation and does not automatically remove lyrics from the task.
- No result refers only to the last query; it does not prove that enrichment is impossible.

Actions are disabled while the editor has unsaved changes. Field coverage does not establish correct identification, timing against vocals or compatibility with a particular player.

When an ASR result is retained, the missing-lyrics explanation includes the fraction of lines aligned in that attempt. A partial result is not a complete LRC; even 100% coverage requires listening against vocals. This is neither queue progress nor confirmation that an earlier reference text is still current.

## Search coverage

When LRCLIB's exact result contains only plain text, the search also checks other variants. If that extra request fails, the plain result is preserved and the search remains incomplete. Cover lookup checks up to three matching album editions, or an explicit MusicBrainz release ID embedded in the file. Artwork remains subject to review; multiple editions no longer suppress the entire lookup.

Catalog explanations use a compact SQL projection without cached artwork, song lyrics or transcripts. Reading reasons does not enqueue processing or modify the collection.

Releases of matching MusicBrainz recordings appear as separate album candidates. Selecting an edition proposes its album name, MusicBrainz release ID and available date while preserving the song title and artist. When the album is missing, cover lookup runs only if all returned editions share one album name; different album names require selection. Nothing is applied automatically. The list is bounded to eight edition candidates.

Fingerprint recognition preserves an existing album in the search query when both embedded title and artist exactly match the recognized identity. Conflicting or missing identity tags use the fingerprint result instead.

An exhausted fingerprint-attempt allowance is distinct from a temporary provider cooldown; a new queue does not reset it. An unknown outcome of a sent request is not a confirmed no-match. Configuring a key does not offer an ineffective recognition retry when the same audio identity has already exhausted its allowance.

If an earlier Shazam search conclusively found no match in every required fragment while AcoustID was unavailable because its connection was disabled or lacked a key, recognition can continue directly with the newly configured AcoustID. The file revision, fragment offsets, extractor profile and sample digests must still match the recorded evidence. Positive, incomplete, conflicting and unknown results are never treated as negatives. The new lookup retains cancellation, provider caching and the same durable attempt allowance; it does not reset or grant calls.

Consistent historical recording IDs can also qualify when their matching verdict is preserved and the old provider tags are no longer available. They remain historical positive evidence. Only the fresh AcoustID result can supply a new fingerprint-based metadata query; expired titles, artists or releases are not restored.
