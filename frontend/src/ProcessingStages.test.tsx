import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ProcessingStages } from './ProcessingStages';
import type { JobActivity, ProcessingStage } from './jobs';

const base: JobActivity = { job: {state:'running'} as JobActivity['job'], server_now: 100,
  active_items: [], active_items_truncated: false, events: [], cursor: 1, resync_required: false, has_more: false };
const stage: ProcessingStage = { stage_id: 'phase', attempt_id: 'attempt', track_id: 'track', filename: 'Test.mp3',
  stage: 'provider_http', provider: 'lrclib', state: 'running', duration_ms: null, elapsed_seconds: 4,
  http_status: null, reason_code: null, long_running: false };

describe('owner-facing processing measurements', () => {
  it('opens a measured trace separately without replacing the app screen', () => {
    const traced = {...stage, trace_id:'a'.repeat(32), trace_url:'https://grafana.example/explore?trace=aaa'};
    const html = renderToStaticMarkup(<ProcessingStages activity={{...base, stage_history:[traced]}} />);
    expect(html).toContain('Подробная трасса в Grafana');
    expect(html).toContain('target="_blank"'); expect(html).toContain('noopener noreferrer');
    expect(html).toContain('Trace ID:');
  });
  it('names the provider and distinguishes unknown duration from a completed zero', () => {
    const html = renderToStaticMarkup(<ProcessingStages activity={{...base, current_stages: [stage], stage_history: [stage]}} />);
    expect(html).toContain('lrclib'); expect(html).toContain('Test.mp3');
    expect(html).toContain('не измерено'); expect(html).toContain('Внутренний прогресс атомарных операций не измеряется');
  });
  it('shows every deferred provider without claiming the worker is hung', () => {
    const html = renderToStaticMarkup(<ProcessingStages activity={{...base, provider_waits: [
      {track_id: 'track', filename: 'Test.mp3', provider: 'lrclib', reason_code: 'rate_limited', retry_at: 200, overdue_wait: false},
      {track_id: 'track', filename: 'Test.mp3', provider: 'musicbrainz', reason_code: 'budget_exhausted', retry_at: null, overdue_wait: false},
    ]}} />);
    expect(html).toContain('lrclib'); expect(html).toContain('musicbrainz');
    expect(html).toContain('Время следующей попытки неизвестно'); expect(html).not.toContain('Срок ожидания прошел');
  });
  it('distinguishes absent measurements and executor failure from success', () => {
    const html = renderToStaticMarkup(<ProcessingStages activity={{...base, executor_health: 'executor_unavailable'}} />);
    expect(html).toContain('Нет ответа исполнителя'); expect(html).toContain('не подтверждение завершения задачи');
    expect(renderToStaticMarkup(<ProcessingStages activity={{...base, server_now: undefined}} />)).toBe('');
  });
});
