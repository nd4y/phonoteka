# Queue recovery and ASR reference words

If the worker exits during metadata analysis, recognition, audio audit, ASR, alignment, ReplayGain or YouTube provenance lookup, startup pauses that job. Use Resume in Processing. Completed items are skipped and interrupted items no longer appear to be running. A cancellation requested before the crash remains effective. Provider budgets and unknown request outcomes are preserved.

Recovery does not publish files or resume network requests by itself. Mutating operations retain their separate file and journal reconciliation; they are not blindly replayed.

An audio audit retains the successfully probed stream inventory even when decoding fails, so a single-stream FLAC can be considered for recovery. Invalid embedded artwork does not prevent creating a verified audio derivative: the original image remains in the preserved source, and the derivative manifest records that it was omitted. No missing audio samples or artwork are fabricated.

Alignment checks only the reference words and their inherited source restrictions. Expired tag or artwork proposals do not block unrelated lyrics. For permitted lyrics already embedded in a file, an HTTP cache TTL is not the lifetime of that file. Revoked permissions and inherited restrictions are still enforced at admission and execution. A pending lyrics proposal retains its cache TTL.

Checks cover alignment admission/rejection, policy cleanup of queued references, the worker's repeated policy check, restart preservation, explicit resume without replaying a completed item, and retained cancellation and provider budgets. Synthetic checks do not establish musical timing accuracy.
