# Russian recognition on the existing ASR engine

The optional `PC2_ASR_RUSSIAN_MODEL=gigaam-v3-e2e-rnnt` profile uses GigaAM for
independent Russian recognition. An explicit Russian language choice goes directly
to GigaAM. Automatic detection first uses the existing multilingual recognizer;
if it detects Russian, recognition is repeated with GigaAM. Other languages keep
the existing Whisper behavior. The default remains unchanged when the variable is
empty. A selected but unavailable or mismatched model fails explicitly.

GigaAM provides real word start/end times and no acoustic word probabilities.
Unknown confidence is displayed as unknown, without synthetic scores. Matching
complete reference lines supplies independent lexical anchors. Whisper still
performs the final attention alignment with the existing mean likelihood,
monotonicity, window and proximity gates. Missing lines are never interpolated.
Even complete coverage remains a draft that requires review; no words, timings,
instrumental classification or file changes are automatically approved.

## Contract and data flow

The client uses the existing authenticated HTTPS engine URL, key and
`/v1/whisper` compatibility prefix. No new public port or authentication exception
is required. The health response advertises the optional recognizer separately;
the request selects its model explicitly. The existing shared GPU lock, queue,
deadlines, process-tree cancellation and scratch cleanup also cover GigaAM.
Each model runs in a disposable process. Models are released between recognition
and alignment. Only analysis PCM is sent for recognition, and the existing short
PCM/reference windows are sent for final alignment. Client source code, metadata
and the collection database stay outside the engine host.

After the owned process tree is reaped, scratch removal retries transient Windows
permission errors up to six attempts with at most 7.4 seconds of delayed backoff.
The GPU lock and active request counters remain held during cleanup. Persistent
permission errors and other filesystem errors are propagated rather than ignored.
Runtime cancellation acceptance checks the actual owned Python process IDs,
GPU memory and scratch files, not just a zero active counter.

The pinned GigaAM package is 0.2.0, model `v3_e2e_rnnt`:

- Checkpoint SHA-256: `f60c62fe45902d967000770a12f260de7c5ac4d4b8e5e852f1df8c548e3958b5`.
- Vocabulary file SHA-256: `828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a`.
- Word probabilities: unavailable. Forced-alignment scores come from Whisper.

Assets must already exist in the engine cache. The worker checks their hashes and
blocks Python network connections instead of downloading missing models during
a request. The enabled resource profile has a new identity so a previous Whisper
result cannot be reused as a GigaAM result for the same audio. Earlier results
and held jobs remain historical records; deployment does not replay them.

## Installation and acceptance

Before updating the existing generic engine, record its exact source hashes and
make a protected backup. Confirm ownership and zero running/waiting ASR work,
including other consumers. Defer the update while it is busy. Install only the
generic `whisper_bridge.py` and `gigaam_model_worker.py` from
`integrations/pc2_asr`; keep the existing server, Whisper worker, models, key,
TLS and service configuration. Do not transfer the application source tree.
Reload only the existing owned service at the verified idle boundary and check
both its ordinary health and authenticated recognizer contract. If that fails,
restore the exact previous bridge and reconcile the owned service before retrying.

Then update the checked application/inference image pair and enable the variable
for inference. Preserve data, queue, tracing and delivery configuration. Rollback
restores the previous image/configuration and generic bridge without replacing
new collection data or deleting prior ASR results.

Acceptance requires the exact CI artifact, negative identity/input/result tests,
unchanged English behavior, cancellation with process/memory/scratch cleanup,
and two real Russian song checks through the UI. Record the exact audio revision,
recognizer, time spent, matched rows and remaining gaps. Synthetic tests prove
contracts, not music accuracy. Installed behavior and real pilots remain separate
from source/build success.

The review screen also distinguishes a current proposal with no new changes from
a proposal for an old file revision. Restricted fields remain restricted; selecting
a permitted, verified source is a separate action.
