import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { observeOwnerActivity } from './ownerActivity';
import type { Session } from './types';

function setup(send: (signal: AbortSignal) => Promise<Session> = vi.fn<(signal: AbortSignal) => Promise<Session>>().mockResolvedValue({ authenticated: true })) {
  vi.useFakeTimers();
  const listeners = new Map<string, EventListener>(); let visible = true, authenticated = true;
  const target = { addEventListener: (name: string, listener: EventListenerOrEventListenerObject | null) => { if (listener) listeners.set(name, listener as EventListener); },
    removeEventListener: (name: string) => { listeners.delete(name); } };
  const received = vi.fn(), expired = vi.fn();
  const stop = observeOwnerActivity({ target, visible: () => visible, authenticated: () => authenticated, send, received, expired });
  return { send, received, expired, stop, listeners,
    input: (trusted = true, name = 'pointerdown') => listeners.get(name)?.({ isTrusted: trusted } as Event),
    hide: () => { visible = false; }, logout: () => { authenticated = false; } };
}
afterEach(() => { vi.useRealTimers(); });

describe('explicit owner activity without background session renewal', () => {
  it('does not renew on elapsed time, synthetic input, hidden input or player/poll events', async () => {
    const s = setup();
    vi.advanceTimersByTime(60 * 60 * 1000);
    s.input(false); s.input(true, 'timeupdate'); s.input(true, 'playing'); s.input(true, 'message');
    s.hide(); s.input();
    expect(s.send).not.toHaveBeenCalled(); s.stop();
  });

  it('sends at most one touch per30seconds and only when another trusted visible input occurs', async () => {
    const s = setup(); s.input(); await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 20; i++) s.input(true, 'keydown');
    expect(s.send).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(30_000); expect(s.send).toHaveBeenCalledOnce();
    s.input(true, 'input'); await vi.advanceTimersByTimeAsync(0); expect(s.send).toHaveBeenCalledTimes(2); s.stop();
  });

  it('aborts and ignores a late response after logout or hook cleanup, including a new login', async () => {
    let resolve!: (session: Session) => void;
    const send = vi.fn((_signal: AbortSignal) => new Promise<Session>(yes => { resolve = yes; }));
    const old = setup(send); old.input(); const signal = send.mock.calls[0][0];
    old.logout(); old.stop();
    resolve({ authenticated: true, csrf_token: 'old-session' }); await vi.advanceTimersByTimeAsync(0);
    expect(signal.aborted).toBe(true); expect(old.received).not.toHaveBeenCalled(); expect(old.expired).not.toHaveBeenCalled();
    const fresh = setup(); fresh.input(); await vi.advanceTimersByTimeAsync(0);
    expect(fresh.received).toHaveBeenCalledWith({ authenticated: true }); fresh.stop();
  });

  it('expires the current session on401 without reviving it or scheduling retries', async () => {
    const s = setup(vi.fn().mockRejectedValue(new ApiError('expired', 401)));
    s.input(); await vi.advanceTimersByTimeAsync(0);
    expect(s.expired).toHaveBeenCalledOnce(); expect(s.received).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60_000); expect(s.send).toHaveBeenCalledOnce(); s.stop();
  });

  it('ignores a late401 from a disposed old session instead of logging out the new session', async () => {
    let reject!: (error: unknown) => void;
    const s = setup(vi.fn((_signal: AbortSignal) => new Promise<Session>((_yes, no) => { reject = no; })));
    s.input(); s.stop(); reject(new ApiError('old expired', 401));
    await vi.advanceTimersByTimeAsync(0); expect(s.expired).not.toHaveBeenCalled();
  });

  it('bounds the request and ignores a response that arrives after abort or session replacement', async () => {
    let resolve!: (value: Session) => void;
    const send = vi.fn((_signal: AbortSignal) => new Promise<Session>(yes => { resolve = yes; }));
    const s = setup(send); s.input();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(send.mock.calls[0][0].aborted).toBe(true);
    resolve({ authenticated: true, csrf_token: 'stale' }); await vi.advanceTimersByTimeAsync(0);
    expect(s.received).not.toHaveBeenCalled(); s.stop();
    const replaced = setup(send); replaced.input(); replaced.logout();
    resolve({ authenticated: false }); await vi.advanceTimersByTimeAsync(0);
    expect(replaced.expired).not.toHaveBeenCalled(); replaced.stop();
  });
});
