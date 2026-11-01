import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, messageFor, setSession } from './api';
afterEach(() => { vi.unstubAllGlobals(); setSession({ authenticated: false }); });
describe('authenticated API client', () => {
  it('sends session CSRF and revision guards with mutations', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'proposal' }), { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);
    setSession({ authenticated: true, csrf_token: 'test-csrf' });
    await api('/tracks/one/proposal', { method: 'POST', headers: { 'If-Match': '"revision"' }, body: JSON.stringify({ tags: { title: 'Название' } }) });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/tracks/one/proposal');
    expect(options.headers.get('X-CSRF-Token')).toBe('test-csrf');
    expect(options.headers.get('If-Match')).toBe('"revision"');
    expect(options.credentials).toBe('same-origin');
    expect(options.cache).toBe('no-store');
  });
  it('surfaces backend message and conflict guidance', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'invalid_lrc', message: 'Строка без времени', retryable: false }), { status: 422 })));
    await expect(api('/jobs', { method: 'POST' })).rejects.toThrow('Строка без времени');
    expect(messageFor(new ApiError('old', 412))).toContain('черновик');
    expect(messageFor(new ApiError('Источник не разрешает экспорт', 409, 'source_policy_denied'))).toBe('Источник не разрешает экспорт');
    expect(messageFor(new ApiError('Поле закреплено владельцем', 409, 'field_locked'))).toBe('Поле закреплено владельцем');
  });
  it('notifies the shell when an authenticated request expires', async () => {
    const target = new EventTarget(); const listener = vi.fn(); target.addEventListener('session-expired', listener);
    vi.stubGlobal('window', target);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    await expect(api('/tracks')).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce();
  });
});
