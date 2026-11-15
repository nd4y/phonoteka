# Trace export performance

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
