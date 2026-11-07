# Why a field is missing

The catalog shows a brief reason below the track name. Open the track to see “What remains to fill”: a separate explanation for each missing title, artist, album, cover and synchronized lyric field. Each row includes a next step, provider and last search time when known.

- An unattempted or stale search offers a search against the current file revision.
- No separate cover-search result does not mean artwork is unavailable online. Check the album and release candidates first.
- Candidates require review; a saved proposal still needs writing to the file. A draft for one field does not explain other missing fields.
- Temporary provider errors or throttling mean the search is incomplete. Retries respect provider cooldowns; IP rotation and quota resets are not used.
- Expired results need refreshing, whereas denied storage or embedding requires checking the provider usage settings. AcoustID that is configured now is not shown as missing a key solely because of an earlier search.
- Plain lyrics can be reviewed and aligned with the audio. A provider's instrumental claim requires confirmation and does not automatically remove lyrics from the task.
- No result refers only to the last query; it does not prove that enrichment is impossible.

Actions are disabled while the editor has unsaved changes. Field coverage does not establish correct identification, timing against vocals or compatibility with a particular player.

When an ASR result is retained, the missing-lyrics explanation includes the fraction of lines aligned in that attempt. A partial result is not a complete LRC; even 100% coverage requires listening against vocals. This is neither queue progress nor confirmation that an earlier reference text is still current.

## Search coverage

When LRCLIB's exact result contains only plain text, the search also checks other variants. If that extra request fails, the plain result is preserved and the search remains incomplete. Cover lookup checks up to three matching album editions, or an explicit MusicBrainz release ID embedded in the file. Artwork remains subject to review; multiple editions no longer suppress the entire lookup.

Catalog explanations use a compact SQL projection without cached artwork, song lyrics or transcripts. Reading reasons does not enqueue processing or modify the collection.
