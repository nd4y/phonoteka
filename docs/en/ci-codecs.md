# CI media dependencies

`dev/install-ci-codecs.sh` checks FFmpeg, FFprobe, Chromaprint's `fpcalc` and loading `libebur128.so.1`. If all four work, CI proceeds without refreshing unrelated package indexes. Otherwise it installs the required packages using existing indexes; a failed installation permits one index refresh and one further installation attempt.

The Ubuntu 24.04 job passes `dev/ubuntu-ci.sources` through `CODEC_APT_SOURCES`. Package commands use only the official Ubuntu HTTPS archive and security repositories, with the standard Ubuntu archive keyring. This avoids a slow HTTP mirror selected by the runner without disabling TLS or package signature checks. The source list applies to those commands only; system repository files are not rewritten. A missing configured list fails before contacting any fallback repository.

Each package command has a three-minute deadline and bounded transport retries. A final executable/library check is mandatory. An unavailable repository or a missing tool fails the job rather than skipping media tests. Package output remains visible to distinguish setup failures from product regressions. This changes only GitHub runner setup, not the application's runtime packages or host configuration.
