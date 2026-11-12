# Fill confirmed fields in bulk

If preparation reports a changed Work device, choose **Reconcile Work folder**. The owner-only scan mode verifies SHA-256 across the entire frozen current catalogue, including CUE and excluded recordings. Only previously missing files may remain absent. The saved path/inode, file signatures, catalogue and authorization must remain unchanged until commit, and at least one existing file must match. A mismatch, cancellation or partial mount leaves the previous binding intact. No audio, proposals, acceptance or scan readiness changes. After success, rebuild the preparation plan. Ordinary scans still reject an unreconciled root.

Open **Needs review → Fill confirmed fields** and run **Check collection**.
This reads saved results without new music-service requests or file writes. The plan shows
before/after values, each field's source, and reasons for remaining gaps. Select records and
submit the batch. **Processing** shows actual job results; submission is not a completed write.

The `confirmed-metadata-fields-v1` contract fills missing title, artist, album and front artwork
only. Existing values, field locks and conflicting saved choices remain intact. A current full
decode audit and agreeing evidence are required:

- a unique AcoustID score of at least 0.98 and a MusicBrainz recording with the same ID and duration within two seconds; or
- agreement between two or three distinct, separated Shazam samples bound to the saved fingerprint by extraction profile, hash and position, and matching MusicBrainz title, artist and duration.

Every compatible official edition must agree on the album name. Artwork requires a selected or
unique edition, or identical image bytes for every compatible edition. Different covers remain
pending even if the album name agrees. A locked or conflicting pending album also blocks artwork
that depends on that album. Mixes, spoken recordings, CUE images and sources already replaced by
derived outputs are excluded.

Authorization, file revision, provider policy, saved proposal, lyric document and publication
review dependencies are checked at enqueue and immediately before the atomic write. Bounded batches
submit IDs and plan tokens rather than a client confidence assertion. Refresh changed plans.
Native history retains backups and provenance; retrying the same request does not repeat its effect.
Unapplied proposal fields, including lyrics, remain a draft for the new file revision.

**Update the ready collection using its accepted profile** carries prior acceptance only across
proven identical audio packets, words and timestamps. The profile, exceptions and listening decisions
are preserved. Without prior acceptance, only the working file changes. Ordinary Ready checks and
permissions still apply; cancelling the parent stops publication that has not happened yet.
The existing service's delivery from Ready to Music remains a separately verified operation.

This mode does not accept new lyrics, timestamps, instrumental classification or listening.
ASR confidence does not establish musical correctness: the existing automatic lyric profile stays
disabled pending a validated music pilot. Reason counts apply to fields and may include one file
more than once. No eligible fields does not establish that further search is impossible.

Server acceptance is covered by `tests/test_confirmed_fields.py`. Build, CI, installed UI and actual
collection delivery have separate acceptance evidence; this document is not a passing result.

Each eligible record has a **Still needs review** section explaining its remaining gaps.
Processing lists the fields actually written by a successful item. An interrupted outer attempt
is not presented as successful even when a durable native commit receipt exists.

## Preparing missing evidence

When saved evidence is insufficient, Check collection offers Prepare data. Expand the upcoming
batch to inspect up to 50 records sharing one search type. Preparation performs two real observations
of only the selected existing files when stability is unconfirmed. It never marks unrelated records
missing or relinks CUE sheets, and it does not shorten the stability grace period. Native audio
recognition or metadata search then refreshes source results. Old fingerprints are not rebound to a new SHA.

Processing provides cancellation, current actions and ETA. The preparation summary survives page reload.
After completion, check the collection again and fill only the newly confirmed fields. Preparation
accepts nothing and changes no audio files, saved proposals or listening decisions. It starts neither
ASR nor automatic lyric acceptance.

IDs, paths, SHA, permissions, Work mount, field locks and saved choices are rechecked. Changed dependencies,
other unfinished work, cancellation, incomplete observations or interruption stop continuation.
Replaying an accepted request returns its original run. A durable attempt receipt for the same revision
and search type prevents automatic repeated spending after restart, failure, cancellation or no match.
An explicit per-record retry remains available. Existing provider permissions, pacing and budgets remain
in effect; IP rotation is not used to evade provider restrictions.

Server checks: `tests/test_metadata_preparation.py`. A completed search does not promise a writable result:
recording conflicts, ambiguous albums and unconfirmed artwork remain visible gap reasons.

Batches prioritize metadata searches using existing evidence, recognition with remaining capacity, then cache checks for exhausted inputs. The preview includes artist, path and the limitation. Native recognition retains the existing cache permissions and expiry; a cache miss cannot send beyond the budget. This outcome remains pending with its reason and does not claim a new match. Genuine audit/file failures still stop continuation. A stale status poll cannot overwrite a launch/cancel response; cancellation of a completed preparation reports its actual completion.

The preparation card shows its current stage and completion without a stale duplicate launch message. Pausing log scrolling affects the event view; the job's actual state is shown above the log.
