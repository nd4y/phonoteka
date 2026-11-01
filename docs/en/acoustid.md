# AcoustID connection

AcoustID supplements Shazam recognition. When there is no unambiguous match and the recording is not marked as a mix, a Recognize job computes Chromaprint and looks up linked MusicBrainz recordings. Multiple matches remain review candidates. Recording versions are not considered verified and tags are never applied automatically.

## UI setup

1. Register an application in [Your Applications](https://acoustid.org/new-application) and obtain an Application API key. The User API key is for submitting fingerprints and is not used here.
2. Sign in as the owner, open Settings and find the AcoustID card with the music sources. Enter the key, enable AcoustID and save the connection.
3. Test the connection. The application sends a synthetic fingerprint; a successful response with no match also confirms API access and key acceptance. The check spends one request from the daily allowance.
4. Run Recognize for selected files. Saving the connection does not create jobs or replay the collection.

Transmission additionally requires server-side `EXTERNAL_FINGERPRINTS=true`. The UI explains a deployment-level denial. Save edits before testing. Leaving the key field empty preserves the existing key; removing the key also disables the integration.

## Data, limits and failure handling

HTTPS POST sends the application key, Chromaprint from at most the first 120 seconds, and the complete file duration. Audio, filenames and existing tags are not transmitted. A shared app/worker admission clock enforces a minimum 0.4-second interval. The daily quota and shared three-send recognition allowance per audio identity remain in force. A new provider does not reset the allowance. Exhaustion requires an owner decision; an unknown sent-request outcome is never retried automatically.

The key is stored in `/state/integrations/acoustid.json` with mode0600 inside a mode0700 directory. Updates are atomic and stale concurrent forms are rejected. API responses expose only key presence, state and configuration version. The key is absent from URLs, the database, caches, events and diagnostics. Include the connection file only in protected state backups. Updating the image while retaining `/state` preserves the connection; no key-specific compose change is needed.

Low scores and missing MusicBrainz links do not produce reliable matches. API errors, throttling and rejected access remain distinct from a no-match result. Up to eight candidates with scores of at least0.8 are retained. This threshold filters suggestions; it is not a probability of correctness. Source usage policy applies to the candidates. MusicBrainz metadata and AcoustID mappings have separate terms from audio and artwork.

References: [API](https://acoustid.org/webservice), [data and licensing](https://acoustid.org/database). The free public API is intended for non-commercial use.
