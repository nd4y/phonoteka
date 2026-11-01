import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ScheduleWaiting } from './SchedulePanel';

describe('unattended input waiting state', () => {
  it('shows the actual waiting-file count and why downstream work has not started', () => {
    const markup = renderToStaticMarkup(<ScheduleWaiting value={{ waiting_inputs: 7, waiting_reason: 'input_stabilizing' }}/>);
    expect(markup).toContain('role="status"');
    expect(markup).toContain('Ожидание завершения загрузки файлов: 7');
    expect(markup).toContain('два завершенных сканирования');
    expect(markup).toContain('период стабильности');
    expect(markup).not.toContain('ошибка');
  });

  it('distinguishes notification backpressure and hides an absent wait state', () => {
    expect(renderToStaticMarkup(<ScheduleWaiting value={{ waiting_inputs: 0, waiting_reason: 'webhook_queue_full' }}/>)).toContain('Ожидание свободного места в очереди уведомлений');
    expect(renderToStaticMarkup(<ScheduleWaiting value={{ waiting_inputs: 0 }}/>)).toBe('');
  });
});
