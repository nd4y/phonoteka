# Queue recovery and ASR reference words

Processing shows active and recoverable jobs before history. History uses the last update time so that a recent result remains visible when the job started much earlier.

In **Sources**, open the timing, audio and loudness actions and set **Recording language** if automatic detection gets a song wrong. The choice applies to the next transcription or alignment request from that track editor.

ASR uses decoded sample duration for transcription and alignment. A single-stream raw MP3 can have an inaccurate bitrate/header duration estimate; its result retains both reported and decoded durations in `duration_evidence`. A substantial mismatch in other containers or multiple streams remains an error. Decoding permits a one-second sentinel beyond the profile limit, then rejects excess samples before loading them or calling PC2. It never accepts a silently truncated 30-minute result. The conservative preflight rejection for a reported duration above 30 minutes remains. No source bytes, model weights, confidence gates or automatic acceptance rules change.

If the worker exits during metadata analysis, recognition, audio audit, ASR, alignment, ReplayGain or YouTube provenance lookup, startup pauses that job. Use Resume in Processing. Completed items are skipped and interrupted items no longer appear to be running. A cancellation requested before the crash remains effective. Provider budgets and unknown request outcomes are preserved.

Recovery does not publish files or resume network requests by itself. Mutating operations retain their separate file and journal reconciliation; they are not blindly replayed.

An audio audit retains the successfully probed stream inventory even when decoding fails, so a single-stream FLAC can be considered for recovery. Invalid embedded artwork does not prevent creating a verified audio derivative: the original image remains in the preserved source, and the derivative manifest records that it was omitted. No missing audio samples or artwork are fabricated.

The cover indicator validates the front image selected for playback using the same bounded JPEG/PNG decoder as image import. A picture frame containing an HTML error page or an unsupported format such as GIF is reported as missing usable artwork with `artwork_warning=invalid_cover`. The bytes and other picture slots are preserved. Rescan existing records to refresh the indicator; metadata analysis can then look for a replacement. This check does not change the image format or approve a proposed cover.

Alignment checks only the reference words and their inherited source restrictions. Expired tag or artwork proposals do not block unrelated lyrics. For permitted lyrics already embedded in a file, an HTTP cache TTL is not the lifetime of that file. Revoked permissions and inherited restrictions are still enforced at admission and execution. A pending lyrics proposal retains its cache TTL.

Checks cover alignment admission/rejection, policy cleanup of queued references, the worker's repeated policy check, restart preservation, explicit resume without replaying a completed item, and retained cancellation and provider budgets. Synthetic checks do not establish musical timing accuracy.

If SQLite reports a busy or locked catalog after ASR returns, the worker makes up to three attempts to store the same result. It checks authorization and the file revision each time. If it still cannot save, the job reports that explicitly. UI requests blocked by a busy catalog return a retryable message. Check whether the operation took effect before repeating it.

Ready publication recovery returns the operation ID and revision SHA-256 to readers. Old recovered receipts are supplemented from history only when the job, track, operation kind and available revision evidence match a unique history row. This read does not repeat publication or edit old receipts. The Music relay still verifies its existing publication, revision and approval gates before copying.
