import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { JobsView } from './JobsView';
import type { Job } from './types';

const job: Job = { id: 'cancelled-job', kind: 'analyze', state: 'cancelled', stage: 'cancelled', total: 10,
  completed: 0, failed: 0, progress: 0, cancel_requested: true, created_at: '', updated_at: '', error: 'cancelled' };
const render = (value: Job) => renderToStaticMarkup(<JobsView jobs={[value]} refresh={async () => {}} onCancel={async () => {}} />);

describe('cancelled task presentation', () => {
  it('shows an ordinary cancellation without a red error alert', () => {
    const html = render(job);
    expect(html).toContain('Отменено'); expect(html).toContain('job-card--cancelled');
    expect(html).not.toContain('role="alert"'); expect(html).not.toContain('>cancelled<');
  });
  it('keeps real failures visible even when cancellation followed completed items', () => {
    const html = render({ ...job, state: 'cancelled_partial', completed: 2, failed: 1, error: 'invalid_audio' });
    expect(html).toContain('Отменено частично'); expect(html).toContain('invalid_audio');
    expect(html).toContain('role="alert"'); expect(html).toContain('с ошибками');
  });
});
