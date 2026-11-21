# Trace export performance

## Cancelled timers

Completing a short attempt releases its state reference without waking the
scheduler for a cancelled timer. The next admission removes cancelled heap
heads. Deadline comparison retains the previous wait boundary: moving the head
later needs no wake, while an earlier live deadline still wakes the thread.
Business operations, segment duration and continuation links stay unchanged.

`tests/test_trace_deadline_admission.py` checks bounded admission while the
consumer is busy and both cancelled/live deadline orders. The regression must
fail on the original module and pass on the candidate. This demonstrates a
specific bookkeeping cost. Shared-runner p95 variability is assessed separately;
one negative overhead measurement or profiler result does not establish its
resolution.

## Wire format and bounds

Serialization runs in turns of at most 16 spans, yielding the background thread
between turns so a large batch cannot monopolize Python's GIL. Encoded packets
still share one HTTP batch. An encoding failure discards that entire batch with
explicit counters; no partly encoded batch is reported as exported. Admission
wakes the exporter only when the queue first reaches its drain threshold.
The real-SDK regression checks decoded equivalence across instrumentation
scopes, failure accounting and recovery on the next batch.

The exporter serializes spans sharing resource and instrumentation-scope metadata
together. Identifiers, parents, links, timestamps, events, attributes and status
remain intact. This changes OTLP packing and adds no settings. Jaeger and HTTP/DNS
coverage stay unchanged.

Metadata copies discard cloned spans before copying scope metadata again.
Small packets use one serialization pass for their exact wire-size check.
Short plain ASCII attribute strings retain the same byte limit without an
unnecessary UTF-8 encode/decode round trip; Unicode and string subclasses keep
their previous normalization. Input protobuf messages remain unchanged.

On Linux the exporter lowers only its own thread's CPU priority. The application
thread retains its priority. If OS policy denies the priority change, export
continues at its previous priority. Other processes are unaffected.

A small group fitting within 8 KiB, including metadata, proves each contained
span also meets the previous limit. Larger groups retain the exact standalone
message check for every span. HTTP packets stay at or below 480 KiB.
Queue count and 8 MiB reservation
bounds remain in force. Counters track spans rather than grouped messages. mTLS,
partial-success handling and bounded shutdown are preserved.

`tests/test_trace_wire_batches.py` compares decoded contents with the previous
format, including multiple resources and scopes, unknown protobuf fields, the
8 KiB boundary, large-packet splitting and live exporter counters. Existing
HTTP/DNS, context isolation and long-trace continuation are covered by
`tests/test_tracing.py`.

The existing `tests/tracing_budget.py` remains the performance gate: 500 generated
files per mode, 4 workers, two available CPUs, at most 5% p95 overhead and
throughput loss, and at most 32 MiB additional RSS. Mandatory CI also retains a
real 600-second receiver outage. A negative overhead on a shared host does not
by itself prove acceleration. Native tests, CI, the installed revision and trace
readback are verified separately.

The exporter reuses the encoded resource header within one batch of up to 128 spans. This applies to groups sharing a single service resource and an instrumentation scope without attributes. The pinned SDK serializes each span, including its events and links. A regression test compares packet bytes across resource and scope changes.

A completed file attempt releases its root span. Work resumed in a copied context can still use the original parent identity in Jaeger. An ordinary file completion no longer needs cyclic garbage collection to release the root.

Successful local TCP phases can reuse the constant protobuf fields inside the same bounded batch. Distinct IDs and timestamps remain per span. Exceptions, extra attributes, remote parents, events, links and tracestate go through the pinned SDK.
