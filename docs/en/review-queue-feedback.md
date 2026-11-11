# Review queue feedback

Skip opens the next record in the captured queue. While it is loading, its button says that the next record is opening. It does not show a file-writing message or submit an apply job.

Write and next keeps its writing message while the explicitly requested apply operation is pending. Both transitions prevent another review action until they finish. The existing queue boundary, cancellation checks, data acceptance and publication workflow are unchanged.

Acceptance checks a delayed next-record read: the transition remains visible and no apply request is sent. Existing workflow tests cover queue boundaries and pending writes; the installed UI is checked separately from CI.
