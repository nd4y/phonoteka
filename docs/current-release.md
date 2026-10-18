# Current implementation: 0.3 alpha

The application is a single-owner service. Original audio is mounted read-only. Changes are proposed, reviewed, atomically written to the separate working copy, audited, and published to a separate Ready directory. The current image is not a claim that all specification acceptance criteria have passed.

## Available

- Async bounded file workers, durable jobs, idempotency, restart recovery, revisions, file history and rollback.
- Native read/write profiles tested with audio packet and unrelated metadata preservation: MP3 ID3v2.3/v2.4, FLAC, M4A/MP4, Ogg Vorbis, Opus, WAV and AIFF. No audio re-encoding for tag changes.
- Discovery checks bounded file signatures as well as extensions, including extensionless MP3 caches. Detected container controls preview MIME. Working filenames stay unchanged; Ready adds the verified extension when absent or misleading, with existing destination conflict checks.
- Additional local FFmpeg demuxer profiles are read-only. Invalid files remain in the catalog with an explicit error. Playlists and remote input URLs are not decoded.
- CUE parsing, encodings, single/multiple FILE, INDEX preview, guarded album images. Sample-accurate CUE extraction creates separate FLAC/PCM WAV files with provenance, atomic album commit and PCM hashes; derivatives support normal tagging and publication. See [recognition and CUE export](recognition.md).
- Free MusicBrainz metadata search, LRCLIB plain/synchronized lyrics, Cover Art Archive and local front artwork. One candidate per kind becomes a proposal. Plain text stays untimed and cannot silently become synchronized lyrics.
- Provider caching bounded to 256 MiB, daily request budget, rate spacing, retries and outage cooldown. No audio is uploaded by these public metadata searches. Missing keys are explicit; text search is not audio fingerprint identification.
- Separate CPU ASR image with pinned faster-whisper small weights. Durable idempotent jobs, input SHA checks, one inference at a time, independent ordinary queue, CPU 2 / RAM 3 GiB. Local decoding to 16 kHz PCM, up to 30 minutes, original timestamps.
- Known reference words are aligned through Whisper cross-attention and DTW in bounded windows selected from independent ASR anchors. Lines without independent anchors or sufficient alignment confidence retain no timestamp. The reference is frozen at job creation. Musical timing quality has not passed the specification pilot; every result requires review.
- Authenticated collection reports, exact duplicates, verified packet duplicates, album track-number gaps inferred from tags, CSV/JSON export. No duplicate deletion or claim of complete release identification.

Track ReplayGain analysis produces gain/peak tag proposals and uses the existing packet-preserving writer and rollback. Album gain is not yet implemented.

## Deployment

Copy `.env.example`, configure disjoint local paths and the two immutable tested image digests. Create an owner password and a separate random inference key in protected files readable only by the configured UID, and create the state/inference directories owned by it. Set EXTERNAL_METADATA=true to permit public metadata requests. EXTERNAL_FINGERPRINTS=true separately enables ShazamIO fingerprint requests. MUSICBRAINZ_HOST accepts only musicbrainz.org (default) or the official beta.musicbrainz.org endpoint. The beta server uses the same live database with prerelease server code; see [MusicBrainz documentation](https://musicbrainz.org/doc/MusicBrainz_Server/Internationalization). HTTPS verification and shared provider request limits remain enforced. INFERENCE_URL=http://inference:8001 enables the local ASR service. Never expose port 8001; only app is published through the authenticated internal reverse proxy. The inference service has read-only access to the working collection, no original or publication mount, and no owner password.

GitHub verifies real codecs, frontend, container behavior and real model inference before publishing either image. The model is included in the inference image; production does not download weights. Update only with idle jobs and a checked backup of state, secrets, storage guard and inference state. Restore the previous compose and matching checkpoint for rollback, preserving separate media history. Additive database tables retain schema 1 compatibility.

## Remaining acceptance boundaries

Opt-in ShazamIO fingerprint recognition works without an API key; the unofficial external service can be unavailable. Current free sources can return no match or an unavailable service. ASR does not establish the artist. A corrupted file cannot be reconstructed from a filename. Physical offline Poweramp playback is a separate device check. The full v1 specification still includes unvalidated musical alignment quality gates, album ReplayGain, broader provider adapters, delivery confirmation and scoped automation API.
