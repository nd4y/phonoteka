import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import type { Session } from './types';

const inputEvents = ['pointerdown', 'keydown', 'input', 'wheel', 'touchstart'];
type ActivityOptions = {
  target: Pick<EventTarget, 'addEventListener' | 'removeEventListener'>;
  visible: () => boolean; authenticated: () => boolean;
  send: (signal: AbortSignal) => Promise<Session>;
  received: (session: Session) => void; expired: () => void;
  now?: () => number; intervalMs?: number;
};

export function observeOwnerActivity({ target, visible, authenticated, send, received, expired,
  now = Date.now, intervalMs = 30_000 }: ActivityOptions) {
  let stopped = false, busy = false, lastAttempt = -Infinity;
  let controller: AbortController | undefined;
  let requestTimeout: ReturnType<typeof setTimeout> | undefined;
  const interval = Number.isFinite(intervalMs) ? Math.max(30_000, Math.min(300_000, intervalMs)) : 30_000;
  const input: EventListener = event => {
    if (stopped || !event.isTrusted || !visible() || !authenticated() || busy || now() - lastAttempt < interval) return;
    lastAttempt = now(); busy = true; controller = new AbortController();
    const current = controller;
    const timeout = requestTimeout = setTimeout(() => current.abort(), 10_000);
    void send(current.signal).then(value => {
      if (stopped || current.signal.aborted || !authenticated()) return;
      if (value.authenticated) received(value); else expired();
    }).catch(error => {
      if (!stopped && !current.signal.aborted && authenticated() && error instanceof ApiError && error.status === 401) expired();
    }).finally(() => { clearTimeout(timeout); if (controller === current) { busy = false; controller = undefined; requestTimeout = undefined; } });
  };
  for (const name of inputEvents) target.addEventListener(name, input, { passive: true });
  return () => { stopped = true; clearTimeout(requestTimeout); controller?.abort(); for (const name of inputEvents) target.removeEventListener(name, input); };
}

export function useOwnerActivity(session: Session | null, authenticated: () => boolean, received: (value: Session) => void) {
  const sessionKey = session?.authenticated ? session.csrf_token : undefined;
  const current = useRef({ authenticated, received, sessionKey }); current.current = { authenticated, received, sessionKey };
  const dispose = useRef(() => {});
  const [restart, setRestart] = useState(0);
  useEffect(() => {
    if (!session?.authenticated) return;
    const stop = observeOwnerActivity({ target: window,
      visible: () => document.visibilityState === 'visible' && !document.hidden,
      authenticated: () => current.current.sessionKey === sessionKey && current.current.authenticated(),
      send: signal => api<Session>('/auth/activity', { method: 'POST', body: '{}', signal }),
      received: value => current.current.received(value),
      expired: () => window.dispatchEvent(new Event('session-expired')),
      intervalMs: (session.activity_interval_seconds ?? 30) * 1000 });
    dispose.current = stop;
    return stop;
  }, [session?.authenticated, session?.csrf_token, session?.activity_interval_seconds, restart]);
  return { stop: () => dispose.current(), resume: () => setRestart(value => value + 1) };
}
