"""Cancelled timer bookkeeping stays bounded without delaying live intervals."""
import heapq
import time
import uuid
from unittest.mock import patch

from phonoteka import tracing
from test_tracing import spans


def test_cancelled_short_timers_do_not_accumulate_or_request_redundant_wakes():
    condition = tracing._deadline_condition
    # Keep the consumer paused so admission/cancellation are deterministic;
    # only cancelled entries from completed tests may be removed here.
    with condition:
        while tracing._deadlines and tracing._deadlines[0][2].state is None:
            heapq.heappop(tracing._deadlines)
        assert not tracing._deadlines
        with patch.object(condition, 'notify', wraps=condition.notify) as notify:
            for _ in range(1000):
                timer = tracing.SegmentDeadline({'started': time.monotonic(), 'number': 1})
                timer.cancel()
                assert timer.state is None
            assert len(tracing._deadlines) == 1
            assert notify.call_count == 1


def test_earlier_live_deadline_wakes_after_a_later_timer_is_cancelled(spans, monkeypatch):
    monkeypatch.setattr(tracing, 'SEGMENT_SECONDS', 60)
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        pass
    spans.clear()
    monkeypatch.setattr(tracing, 'SEGMENT_SECONDS', .04)
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        with tracing.span('still.running'):
            stop = time.monotonic() + 2
            while not spans.get_finished_spans() and time.monotonic() < stop:
                time.sleep(.005)
            assert any(s.name == 'still.running' for s in spans.get_finished_spans())
    result = [s for s in spans.get_finished_spans() if s.name == 'still.running']
    assert result[0].end_time - result[0].start_time <= 40_000_000


def test_cancelling_earlier_head_does_not_delay_later_live_deadline(spans, monkeypatch):
    monkeypatch.setattr(tracing, 'SEGMENT_SECONDS', .03)
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        pass
    spans.clear()
    monkeypatch.setattr(tracing, 'SEGMENT_SECONDS', .06)
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        with tracing.span('live.after.cancelled'):
            stop = time.monotonic() + 2
            while not spans.get_finished_spans() and time.monotonic() < stop:
                time.sleep(.005)
            assert any(s.name == 'live.after.cancelled' for s in spans.get_finished_spans())
    result = [s for s in spans.get_finished_spans() if s.name == 'live.after.cancelled']
    assert result[0].end_time - result[0].start_time <= 60_000_000
