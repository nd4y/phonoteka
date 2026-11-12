# Fill confirmed fields in bulk

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
