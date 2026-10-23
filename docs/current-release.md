# Current implementation: 0.7.0 alpha

The application is a single-owner service. Original audio is mounted read-only. Changes are proposed, reviewed, atomically written to the separate working copy, audited, and published to a separate Ready directory. The current image is not a claim that all specification acceptance criteria have passed.

Production revision verified on 2026-10-23 (Europe/Moscow): `ff62cf4d1fddd1ed97ffe5cab7d2915ec822fab4`. All three installed containers are healthy with zero restarts/OOM. The [PC2 ASR delivery](pc2-asr-delivery-2026-10-23.md) activates the existing independent GPU engine without exporting Phonoteka sources, fixes cancellation and CPU/cgroup budget admission, and preserves the earlier [Poweramp fix](poweramp-pc2-review-2026-10-23.md). [GitHub39883422671](https://github.com/nd4y/phonoteka/actions/runs/39883422671) passed639 backend/210 frontend tests,63 exact installed modules and19ASR/7combined/15ordinary criteria. Actual installed full-track jobs returned partial timing drafts; full musical acceptance and collection organization remain open. See [release readiness](production-readiness.md).

The owner-authorized [Edge acceptance](browser-acceptance-2026-10-22.md) verified playback/CUE, Back, reload state, dirty-form protection, real audit cancellation/live logs and the four agreed responsive sizes. The owner separately confirmed PWA installation and launch as a separate application. The later [guide review](collection-guide-review.md) verified a controlled origin outage with a cached public shell, reconnect and logout during playback. The later owner-selected Poweramp1031/Android16 emulator acceptance verified 20 generated MP3/FLAC offline, embedded lyrics/art/tags, ReplayGain and M3U8: see [native acceptance](poweramp-pc2-review-2026-10-23.md). Acoustic output on a physical device is not inferred.

## Available

- Live job subscriptions recover permanently closed SSE connections from a fresh snapshot; task changes cannot display another task's log. Queue responses cannot overwrite a newer cancellation, and logout invalidates pending private results. Normal cancellation remains distinct from a real task failure.

- Accessible audio preview controls shared by normal tracks and CUE: play/pause, stop, bounded seeking and 10-second jumps, exact time entry, volume/mute, 0.5x-2x speed and repeat. CUE controls show composition-relative time and remain within its boundaries. Lyrics editing uses the same media position. Volume, mute and speed are stored locally; keyboard shortcuts apply only inside the focused player and do not intercept editor inputs. Browser decoding support still determines playable formats.

- Dedicated-token Prometheus metrics for API/worker/inference, aggregate SQLite queue/collection/provider gauges, process counters and duration histograms; JSON stdout logs with generated request IDs and no user file paths or secrets. A reproducible Grafana dashboard with 63 verified queries includes metrics and VictoriaLogs. See [observability](observability.md).

- YouTube provenance from embedded URLs, yt-dlp filenames and bounded sidecars, inherited source links for derivatives, owner confirmation/unlink, public video metadata and up to three search candidates using pinned yt-dlp without media downloads/cookies. Search does not establish the exact original upload. See [YouTube provenance](youtube.md).

- Async bounded file workers, durable jobs, idempotency, restart recovery, revisions, file history and rollback.
- Native read/write profiles tested with audio packet and unrelated metadata preservation: MP3 ID3v2.3/v2.4, FLAC, M4A/MP4, Ogg Vorbis, Opus, WAV and AIFF. No audio re-encoding for tag changes.
- Discovery checks bounded file signatures as well as extensions, including extensionless MP3 caches. Detected container controls preview MIME. Working filenames stay unchanged; Ready adds the verified extension when absent or misleading, with existing destination conflict checks.
- Additional local FFmpeg demuxer profiles can be converted to separate writable FLAC/PCM WAV derivatives with exact decoded PCM verification and retained sources. Invalid files remain in the catalog with an explicit error. Playlists and remote input URLs are not decoded.
- CUE parsing, encodings, single/multiple FILE, INDEX preview, guarded album images. Sample-accurate CUE extraction creates separate FLAC/PCM WAV files with provenance, atomic album commit and PCM hashes; derivatives support normal tagging and publication. See [recognition and CUE export](recognition.md).
- Free MusicBrainz metadata search, LRCLIB plain/synchronized lyrics, Cover Art Archive and local front artwork. One candidate per kind becomes a proposal. Plain text stays untimed and cannot silently become synchronized lyrics.
- Provider caching bounded to 256 MiB, daily request budget, rate spacing, retries and outage cooldown. No audio is uploaded by these public metadata searches. Missing keys are explicit; text search is not audio fingerprint identification.
- Separate supervised inference service, currently using the pinned PC2 Large-v3 GPU engine over authenticated HTTPS. Source decoding and conservative line decisions remain on NAS; no Phonoteka source export or local model weights. Optional CPU small/int8 profile requires3GiB before loading weights. Durable idempotent jobs, SHA checks, serial model work, independent ordinary queue, bounded PCM/results and active cancellation. Up to30 minutes, original timestamps. See [resource profiles](asr-resource-profile.md).
- Known reference words are aligned through Whisper cross-attention and DTW in bounded windows selected from independent ASR anchors. Lines without independent anchors or sufficient alignment confidence retain no timestamp. The reference is frozen at job creation. Musical timing quality has not passed the specification pilot; every result requires review.
- Authenticated collection reports, exact duplicates, verified packet duplicates, album track-number gaps inferred from tags, CSV/JSON export. No duplicate deletion or claim of complete release identification.

Track ReplayGain analysis produces gain/peak tag proposals and uses the existing packet-preserving writer and rollback. Album gain uses libebur128 joint gating across the explicitly selected group, with per-track and common album gain/true-peak tags. The group is not proof of release completeness.

## Deployment

Copy `.env.example`, configure disjoint local paths and the two immutable tested image digests. Create an owner password and a separate random inference key in protected files readable only by the configured UID, and create the state/inference directories owned by it. Set EXTERNAL_METADATA=true to permit public metadata requests. EXTERNAL_FINGERPRINTS=true separately enables ShazamIO fingerprint requests. MUSICBRAINZ_HOST accepts only musicbrainz.org (default) or the official beta.musicbrainz.org endpoint. The beta server uses the same live database with prerelease server code; see [MusicBrainz documentation](https://musicbrainz.org/doc/MusicBrainz_Server/Internationalization). HTTPS verification and shared provider request limits remain enforced. INFERENCE_URL=http://inference:8001 enables the local ASR service. Never expose port 8001; only app is published through the authenticated internal reverse proxy. The inference service has read-only access to the working collection, no original or publication mount, and no owner password.

GitHub verifies real codecs, frontend, container behavior and real model inference before publishing either image. The model is included in the inference image; production does not download weights. Update only with idle jobs and a checked backup of state, secrets, storage guard and inference state. Restore the previous compose and matching checkpoint for rollback, preserving separate media history. The deployed database uses schema 2. Follow the explicit offline migration/checkpoint procedure; old workers must not write to the migrated state.

## Remaining acceptance boundaries

Opt-in ShazamIO fingerprint recognition works without an API key; the unofficial external service can be unavailable. Current free sources can return no match or an unavailable service. ASR does not establish the artist. A corrupted file cannot be reconstructed from a filename. Physical offline Poweramp playback is a separate device check. The full v1 specification still includes unvalidated musical alignment quality gates, broader physical format/device acceptance and musical ASR quality. Optional Discogs/TheAudioDB adapters are implemented but disabled unless explicitly configured; their mocked contracts do not prove live keyed access. Scoped API tokens, schedules and revision-bound delivery receipts are available. Device receipts distinguish owner/client reports from NAS-side verification.

## Automation and reliable retries

Scoped expiring bearer tokens, owner-only token management, a durable opt-in scan/recognize/analyze/ReplayGain schedule, verified Ready manifests and portable M3U8 playlists are implemented. See [automation](automation.md). Shazam 429 and provider outages defer the job with a persisted retry time rather than marking all skipped files complete. Finished items survive retry/restart. Daily budgets are not reset to force completion. Failed ASR requests receive a new generation on explicit retry.

Jobs support pause/resume at file boundaries. Offline state backup/restore verifies artifacts, excludes active tokens/sessions/cache, starts recovery mode and requires an administrative work/Ready hash check before new effects. Scratch cleanup defaults to preview and keeps rollback/exports/music. See [maintenance](maintenance.md). Real-file edge cases include a zero native MP3 duration (FFprobe fallback) and an undecodable advertised tail (bounded sample from the beginning).


## Delivered guide/report

[f41bba9 review](collection-guide-review.md): built and tested on build-01, 210 frontend/105 backend regressions, generated-data Edge checks of shared wizard/help history, immediate F5, responsive report, controlled origin502/cached public shell/reconnect and logout with playing WAV. The update has been delivered after full matched CI and deployment verification; operational details are kept separately. No external ASR, cloud source or OTLP feature was added.


## CHG-044: выпуск проверен

Справка, мастер организации, понятный отчет и исправления PWA/Back/F5/logout реализованы и выпущены. [CI39798349961](https://github.com/nd4y/phonoteka/actions/runs/39798349961) для f41bba9 прошел:613backend/210frontend,19ASR/7combined/15ordinary. Проверки установленной версии через HTTPS пройдены; подробный журнал развертывания хранится отдельно от репозитория. Физическая проверка Android/Poweramp и независимая музыкальная приемка остаются открытыми.
