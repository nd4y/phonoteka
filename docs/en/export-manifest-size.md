# Ready collection manifest size

Snapshot creation and reading share a 32 MiB UTF-8 manifest limit. Source history,
usage permissions and review evidence are preserved. Previously, a successfully
created manifest above 10 MiB could incorrectly return "Export not found".
After updating, that snapshot is accessible without copying its audio again.

A new manifest is checked before copying files. Exceeding the limit leaves the
previous snapshot unchanged; an oversized existing manifest returns a distinct
size error. Reads are byte-bounded, including when a file grows during the read.
Path, authorization, source-policy and checksum checks remain in place.

Regression tests access a snapshot above 10 MiB over HTTP, download its audio and
record the download verification. Negative tests check refusal before copying
and preservation of the previous snapshot. This does not claim support for
unbounded collection sizes or Poweramp acceptance.
