# Media integrity contracts

AUD-01 uses `full_decode_all_audio_streams`: ffprobe inventories every declared
audio stream, packet verification requires packets for each declared stream,
and FFmpeg fully decodes `0:a`. A corrupt secondary stream cannot be hidden by
a successful first-stream decode. Container bytes are unchanged by this audit.

An audit exposes `audio_stream_count`, `audio_streams`, duration, analyzer,
profile, full input SHA-256, and `audio_identity_scheme`. Single-stream packet
identities retain the existing v1 representation. Multiple streams use
`encoded_audio_packets_v2_all_streams`, binding each packet and its side data to
its audio-stream ordinal. This hash is separate from the full file SHA-256 and
from a recognition fingerprint. Signal measurements, browser preview and
external recognition still use the first audio stream; the audit says so
explicitly for a multistream container. Decoder warnings produce a bounded
diagnostic code rather than including private paths or unbounded log text.
The normal PCM mono/stereo message `Guessed Channel Layout for Input Stream`
is retained as `decoder_channel_layout_inferred` in notes. Every other decoder
message remains a warning; fatal decoding errors still fail the audit.
The matching covers the observed FFmpeg 5 input-stream form and FFmpeg 6.1.1
`[aist#.../codec @ 0x...] Guessed Channel Layout: mono/stereo` form. A single
bounded Ubuntu 24.04 diagnostic generated the exact four signal-test WAVs;
each decoded with exit 0 and only that informational note. Version, raw stderr,
image digest and exact-container cleanup evidence are retained in the release
acceptance output. Multichannel guesses and text containing additional error
details are not classified as informational.

Before external fingerprint recognition, Worker requires a server audit with
`state=passed`, current full SHA-256, this all-stream profile and a valid audio
identity. Missing/legacy evidence triggers a full audit through the bounded I/O
pool. Its result is saved under track/revision CAS before any sample, HTTP
reservation or request. Failure blocks recognition with `audit_failed`. Current
evidence reuses its packet identity without a second packet probe. API proposals
cannot supply this evidence.

LIB-03 exposes nullable `hash_verified_at` in track metadata and list/detail
responses. It is ISO UTC and belongs to the returned full file revision. A
successful full inventory advances it only after the size/mtime/inode stability
check. A failed/incomplete scan does not advance the old record. A changed
external revision receives its own full-read date. Successful mutation
reopening and the final full-hash check of an audit also advance the date.
Legacy records without this evidence return `null`; metadata modification
time and audio fingerprint dates are not substituted for a full hash check.

REC-02 admission gives each full encoded-audio packet identity at most three
fingerprint HTTP reservations, shared by all recognition adapters. This is a
persistent quota separate from daily per-provider request accounting. Tag-only
changes, alternative fragments, new jobs/batches, restarts and eviction of old
attempt-log records do not reset it. Batch IDs remain bounded provenance.
Reserved, sent and unknown outcomes consume allowance; metadata catalogue
lookups do not consume this three-call quota. Admission and both counters
commit in the same transaction before network transmission. Missing verified
identity fails closed. Exhaustion is terminal `needs_review` with reason
`recognition_attempt_budget_exhausted`, without automatic provider/daily retry.
The attempt report exposes bounded quota aggregates; Prometheus exposes only
aggregate spent-call and open/exhausted-identity values, without identity labels.
Permitted cached matches remain usable after the three-call allowance is spent;
only a cache miss checks capacity before invoking the recognition library.
The durable counter covers reservations made after this ledger implementation
was installed. Pre-upgrade call history lacks trustworthy audio-identity links;
it is not reconstructed or presented as a proven historical three-call limit.

State restore relocates history backup/journal handles to artifacts actually
present in the verified snapshot. New snapshots record their source state
root; older v1 snapshots can match a unique `backups/...` artifact suffix.
Unmapped handles become unavailable. A restored state never borrows a backup
from the formerly live state, and music target paths are not rewritten or
restored. An available copied backup remains usable after the old copy is
removed, subject to fresh owner authorization, current-file CAS and recovery
acknowledgement. Immutable export contents and retention journals retain their
bytes during relocation.

Regression fixtures use generated audio and temporary state only. They cover
an unchanged first stream with changed/corrupt second stream, full-read dates,
copied rollback blobs and missing restored blobs. Their execution belongs to
the build-01 test run; source implementation is not a physical power-loss or
real-device acceptance result.
