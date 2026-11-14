# Refreshing stale lyric searches

Collection preparation also includes individual songs whose only missing field is
synchronized lyrics and whose previous lyric search has expired or belongs to an
older file revision. Use **Check collection**, inspect the selected records, and
start **Prepare data**. The reason beside each song explains this metadata-only
lookup. Preparation remains limited to 50 selected records per run.

New lyric-only runs query LRCLIB directly. They do not wait for MusicBrainz,
artwork or optional catalog services when tags and artwork are already present.
Existing evidence for the same file revision keeps its original permissions and
expiry; a lyric lookup does not refresh it. In-progress older runs retain their
original scope and receipts. LRCLIB errors still use the normal paced retry.

This path uses the existing catalog search and its provider permissions, quotas,
cooldowns and authorization checks. It sends no new audio fingerprints and never
resets a spent recognition allowance. Existing preparation receipts, including
stopped or failed attempts, prevent automatic replay. Refreshing results neither
replaces a saved lyric draft nor writes tags, words or timings into audio.

Mixes, spoken recordings, confirmed instrumentals, locked lyrics, existing plain
lyrics and current lyric drafts remain outside this path. Fresh candidates and
partial ASR results require their normal review. Finding all timestamped lines is
not proof that their words or timing match the recording.
