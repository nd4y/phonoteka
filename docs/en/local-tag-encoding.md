# Local tag encoding repairs

A manual correction of title, artist, album or album artist can restore bytes that were decoded with the wrong character set. The proposal records the current file revision, a hash of the original value and the reversible encoding steps. An unrelated, unapplied provider suggestion no longer supplies the provenance of that local correction.

This applies to one existing value and at most two strict Latin-1, UTF-8 or Windows-1251 transformations. It does not select a recording, replace multiple artist values or automatically apply a proposal. The usual revision, field-lock, backup and file verification checks still run.

Restrictions on embedded provider data, inherited restrictions, explicit provider selections, lyrics and artwork remain enforced. No provider permission, cache lifetime or tracing configuration changes. A successful encoding repair does not establish that the existing tags identify the correct performance.
