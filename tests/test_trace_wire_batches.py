"""Generated protobufs only; preserve OTLP data and the previous wire limits."""
from io import BytesIO
import os
import sys
from unittest.mock import patch

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from phonoteka import tracing


def message(resources=1, scopes=1, spans=4, payload=0):
    value = ExportTraceServiceRequest(); number = 0
    for r in range(resources):
        resource = value.resource_spans.add(); resource.schema_url = 'generated-resource-schema'
        resource.resource.attributes.add(key='service.name').value.string_value = f'generated-{r}'
        resource.MergeFromString(b'\xa0\x06\x01')  # Unknown wrapper field survives CopyFrom.
        for s in range(scopes):
            scope = resource.scope_spans.add(); scope.schema_url = 'generated-scope-schema'
            scope.scope.name = f'generated-{s}'; scope.scope.version = '1'
            scope.MergeFromString(b'\xa0\x06\x02')
            for _ in range(spans):
                number += 1; span = scope.spans.add()
                span.trace_id = (r+1).to_bytes(16, 'big'); span.span_id = number.to_bytes(8, 'big')
                span.parent_span_id = b'\x01'*8; span.name = 'http.client'; span.kind = 3
                span.start_time_unix_nano = number; span.end_time_unix_nano = number+1
                span.attributes.add(key='server.address').value.string_value = 'localhost'
                span.attributes.add(key='generated-payload').value.string_value = 'x'*payload
                event = span.events.add(); event.name = 'generated-event'; event.time_unix_nano = number
                span.links.add(trace_id=b'\x02'*16, span_id=b'\x03'*8)
                span.status.code = 1
    return value


def old_single(resource, scope, span):
    """Independent reference: the previous serializer's single-span envelope."""
    value = ExportTraceServiceRequest()
    target = value.resource_spans.add(); target.CopyFrom(resource); target.ClearField('scope_spans')
    group = target.scope_spans.add(); group.CopyFrom(scope); group.ClearField('spans')
    group.spans.add().CopyFrom(span)
    return value.SerializeToString()


def flattened(values):
    result = []
    for value in values:
        for resource in value.resource_spans:
            metadata = type(resource)(); metadata.CopyFrom(resource); metadata.ClearField('scope_spans')
            for scope in resource.scope_spans:
                instrumentation = type(scope)(); instrumentation.CopyFrom(scope); instrumentation.ClearField('spans')
                for span in scope.spans:
                    result.append((metadata.SerializeToString(), instrumentation.SerializeToString(), span.SerializeToString()))
    return result


def parsed(packets):
    return [ExportTraceServiceRequest.FromString(data) for data, _ in packets]


def test_grouping_preserves_every_span_and_all_resource_scope_metadata():
    value = message(resources=2, scopes=2, spans=8)
    packets, dropped = tracing._wire_batches(value)
    old = [old_single(r, s, span) for r in value.resource_spans for s in r.scope_spans for span in s.spans]
    assert dropped == 0 and sum(count for _, count in packets) == 32
    assert flattened(parsed(packets)) == flattened([value])
    assert sum(len(data) for data, _ in packets) < sum(map(len, old))
    assert all(len(data) <= 480*1024 for data, _ in packets)


def test_grouping_does_not_mutate_reused_input_protobuf():
    value = message(resources=2, scopes=2, spans=17, payload=200)
    before = value.SerializeToString()
    first, dropped = tracing._wire_batches(value)
    second, again = tracing._wire_batches(value)
    assert dropped == again == 0 and first == second
    assert value.SerializeToString() == before
    assert flattened(parsed(first)) == flattened([ExportTraceServiceRequest.FromString(before)])


@pytest.mark.parametrize('text,expected', [('a'*256,'a'*256), ('a'*257,'a'*256), ('é'*130,'é'*128), ('🙂'*65,'🙂'*64)])
def test_attribute_utf8_byte_boundary_is_preserved(text, expected):
    assert tracing.safe({'http.route':text,'private.lyrics':'must not export'}) == {'http.route':expected}


def test_string_subclasses_keep_original_attribute_normalization():
    class Text(str): pass
    value = tracing.safe({'http.route':Text('generated')})
    assert value == {'http.route':'generated'} and type(value['http.route']) is str


def test_original_single_span_wire_boundary_includes_metadata():
    value = message(spans=2); resource = value.resource_spans[0]; scope = resource.scope_spans[0]
    low, high = 0, 9000
    while low < high:
        middle = (low+high+1)//2
        scope.spans[0].attributes[1].value.string_value = 'x'*middle
        if len(old_single(resource, scope, scope.spans[0])) <= 8192: low = middle
        else: high = middle-1
    scope.spans[0].attributes[1].value.string_value = 'x'*low
    scope.spans[1].attributes[1].value.string_value = 'x'*(low+1)
    assert len(old_single(resource, scope, scope.spans[0])) <= 8192
    assert len(old_single(resource, scope, scope.spans[1])) > 8192
    packets, dropped = tracing._wire_batches(value)
    assert dropped == 1 and sum(count for _, count in packets) == 1
    expected = ExportTraceServiceRequest.FromString(old_single(resource, scope, scope.spans[0]))
    assert flattened(parsed(packets)) == flattened([expected])


def test_large_valid_batch_splits_without_losing_or_duplicating_spans():
    value = message(spans=128, payload=7600)
    assert all(len(old_single(r, s, span)) <= 8192 for r in value.resource_spans for s in r.scope_spans for span in s.spans)
    packets, dropped = tracing._wire_batches(value)
    assert dropped == 0 and len(packets) >= 2
    assert all(0 < len(data) <= 480*1024 for data, _ in packets)
    assert sum(count for _, count in packets) == 128
    assert flattened(parsed(packets)) == flattened([value])


def test_oversized_resource_metadata_is_still_rejected():
    value = message(spans=1)
    value.resource_spans[0].resource.attributes[0].value.string_value = 'generated-'+'x'*8192
    packets, dropped = tracing._wire_batches(value)
    assert packets == [] and dropped == 1


def test_single_envelope_size_matches_original_at_varint_boundaries():
    for metadata_length in (0, 100, 127, 128, 8000, 8192):
        for payload_length in (0, 1, 100, 127, 128, 7900, 8000, 8192):
            value = message(spans=1, payload=payload_length)
            resource = value.resource_spans[0]; scope = resource.scope_spans[0]
            resource.resource.attributes[0].value.string_value = 'x'*metadata_length
            original = old_single(resource, scope, scope.spans[0])
            packets, dropped = tracing._wire_batches(value)
            if len(original) > 8192:
                assert dropped == 1 and packets == []
            else:
                assert dropped == 0 and sum(count for _, count in packets) == 1
                assert flattened(parsed(packets)) == flattened([ExportTraceServiceRequest.FromString(original)])


@pytest.mark.parametrize('deny_priority', (False, True))
def test_processor_accounts_for_spans_in_grouped_packets_not_packet_count(monkeypatch, deny_priority):
    value = message(spans=128, payload=7000)
    value.resource_spans[0].scope_spans[0].spans[-1].attributes[1].value.string_value = 'x'*9000

    class Response:
        status_code = 200
        def __init__(self): self.raw = BytesIO(b'')
        def __enter__(self): return self
        def __exit__(self, *args): pass

    class Session:
        def __init__(self): self.sent = []
        def post(self, endpoint, *, data, **kwargs):
            assert len(data) <= 480*1024
            self.sent.append(ExportTraceServiceRequest.FromString(data))
            return Response()

    session = Session()
    priority = os.getpriority(os.PRIO_PROCESS, 0) if sys.platform == 'linux' else None
    if deny_priority and hasattr(os, 'setpriority'):
        def denied(*args): raise PermissionError('generated-priority-denied')
        monkeypatch.setattr(os, 'setpriority', denied)
    def encode_batch(batch):
        selected = type(value)(); selected.CopyFrom(value)
        group = selected.resource_spans[0].scope_spans[0]; group.ClearField('spans')
        for index in batch: group.spans.add().CopyFrom(value.resource_spans[0].scope_spans[0].spans[index])
        return selected

    with patch.object(tracing, 'encode_spans', side_effect=encode_batch):
        processor = tracing.BoundedProcessor('https://localhost/generated', session)
        try:
            for index in range(128): processor.on_end(index)
            assert processor.force_flush(2000)
            assert processor.accepted == 128 and processor.exported == 127
            assert processor.dropped == 1 and processor.failed == processor.partial == 0
            expected, _ = tracing._wire_batches(value)
            assert flattened(session.sent) == flattened(parsed(expected))
            if priority is not None:
                assert os.getpriority(os.PRIO_PROCESS, 0) == priority
                worker_priority = os.getpriority(os.PRIO_PROCESS, processor.thread.native_id)
                assert worker_priority == (priority if deny_priority else min(19, priority+5))
        finally:
            processor.shutdown(); processor.thread.join(timeout=2)
        assert not processor.thread.is_alive()
