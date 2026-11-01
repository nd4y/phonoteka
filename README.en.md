# phonoteka

Owners can configure AcoustID in the UI: save, replace or remove an Application API key, enable recognition and test the connection. Chromaprint lookup supplements inconclusive Shazam results and produces review candidates. See [setup, transmitted data and limits](docs/en/acoustid.md).

Recognition reports count each file outcome once; old AcoustID key-required markers do not become recognition errors after upgrading.

Development and contribution entry point. Product capabilities, installation and current limitations are described in the [existing product README](README.md). Full translation is tracked as unverified; this page does not claim translation parity yet.

## Development standard

This repository adopts version 0.4.0 of the shared development standard. Read the [English guide](docs/en/development-standard.md) or [Russian guide](docs/ru/development-standard.md). The local toolkit checks staged secrets, changed-file formatting and bilingual documentation before merge. Existing product CI remains in place. Adoption is in progress; product acceptance and legacy-document translation are recorded separately in .repo-policy.json.

## Collection workflow

Start with Overview for the next action, review proposals, check processing outcomes, then transfer ready files. See the [interface workflow](docs/en/collection-workflow.md).

The saved-proposal count is narrower than the full review queue, which also includes errors and conflicts. An empty Ready files section explains how to publish reviewed revisions; an empty search suggests refining the query.
