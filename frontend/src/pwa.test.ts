import { afterEach, describe, expect, it, vi } from 'vitest';

// Observe the store without a DOM renderer. All browser lifecycle events below
// are dispatched independently from the module's implementation.
vi.mock('react', () => ({
  useSyncExternalStore: (_subscribe: unknown, getSnapshot: () => unknown) => getSnapshot(),
}));

class FakeWorker extends EventTarget {
  state: ServiceWorkerState;
  postMessage = vi.fn();
  constructor(state: ServiceWorkerState) { super(); this.state = state; }
  change(state: ServiceWorkerState) {
    this.state = state;
    this.dispatchEvent(new Event('statechange'));
  }
}

class FakeRegistration extends EventTarget {
  installing: FakeWorker | null = null;
  waiting: FakeWorker | null = null;
  active: FakeWorker | null = null;
  update = vi.fn(async () => undefined);
}

async function setup(current = new FakeRegistration(), controller: FakeWorker | null = null) {
  vi.resetModules();
  vi.stubEnv('PROD', true);
  let markReady!: (value: FakeRegistration) => void;
  const serviceWorker = Object.assign(new EventTarget(), {
    controller,
    register: vi.fn(async () => current),
    ready: new Promise<FakeRegistration>(resolve => { markReady = resolve; }),
  });
  const reload = vi.fn();
  vi.stubGlobal('navigator', { onLine: true, serviceWorker });
  vi.stubGlobal('document', { readyState: 'complete' });
  vi.stubGlobal('window', Object.assign(new EventTarget(), {
    isSecureContext: true,
    matchMedia: () => Object.assign(new EventTarget(), { matches: false }),
    location: { reload },
    setTimeout: (callback: () => void, delay: number) => globalThis.setTimeout(callback, delay),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => globalThis.clearTimeout(id),
  }));
  const pwa = await import('./pwa');
  pwa.registerPwa();
  await Promise.resolve();
  await Promise.resolve();
  return { pwa, current, serviceWorker, reload, markReady };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('PWA lifecycle and explicit updates', () => {
  it('makes the first installation offline ready without announcing an update', async () => {
    const { pwa, current, serviceWorker, reload, markReady } = await setup();
    const first = new FakeWorker('installing');
    current.installing = first;
    current.dispatchEvent(new Event('updatefound'));
    current.installing = null;
    current.waiting = first;
    first.change('installed');
    expect(pwa.usePwa()).toMatchObject({ updateReady: false, offlineReady: false });
    current.waiting = null;
    current.active = first;
    first.change('activating');
    serviceWorker.controller = first;
    serviceWorker.dispatchEvent(new Event('controllerchange'));
    first.change('activated');
    markReady(current);
    await Promise.resolve();
    expect(pwa.usePwa()).toMatchObject({ updateReady: false, offlineReady: true, error: null });
    expect(first.postMessage).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it('finds an update already waiting before registration resolves', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    current.waiting = new FakeWorker('installed');
    const { pwa, reload } = await setup(current, current.active);
    expect(pwa.usePwa()).toMatchObject({ updateReady: true, offlineReady: true });
    expect(current.waiting.postMessage).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it('tracks installation of a new version and reloads only after explicit acceptance of that worker', async () => {
    const current = new FakeRegistration();
    const old = new FakeWorker('activated');
    current.active = old;
    const { pwa, serviceWorker, reload } = await setup(current, old);
    const next = new FakeWorker('installing');
    current.installing = next;
    current.dispatchEvent(new Event('updatefound'));
    expect(pwa.usePwa().updateReady).toBe(false);
    current.installing = null;
    current.waiting = next;
    next.change('installed');
    expect(pwa.usePwa().updateReady).toBe(true);
    expect(next.postMessage).not.toHaveBeenCalled();
    const beforeReload = vi.fn();
    const accepted = pwa.activatePwaUpdate({ onBeforeReload: beforeReload });
    expect(next.postMessage).toHaveBeenCalledWith({ type: 'PHONOTEKA_ACTIVATE' });
    serviceWorker.dispatchEvent(new Event('controllerchange'));
    await Promise.resolve();
    expect(reload).not.toHaveBeenCalled();
    current.waiting = null;
    current.active = next;
    next.change('activating');
    serviceWorker.controller = next;
    serviceWorker.dispatchEvent(new Event('controllerchange'));
    await expect(accepted).resolves.toBe(true);
    expect(beforeReload).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
    expect(beforeReload.mock.invocationCallOrder[0]).toBeLessThan(reload.mock.invocationCallOrder[0]);
    expect(pwa.usePwa().updateReady).toBe(false);
  });

  it('clears an update accepted in another tab without reloading this tab', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    const next = new FakeWorker('installed');
    current.waiting = next;
    const { pwa, serviceWorker, reload } = await setup(current, current.active);
    expect(pwa.usePwa().updateReady).toBe(true);
    current.waiting = null;
    current.active = next;
    next.change('activating');
    serviceWorker.controller = next;
    serviceWorker.dispatchEvent(new Event('controllerchange'));
    next.change('activated');
    await Promise.resolve();
    expect(pwa.usePwa()).toMatchObject({ updateReady: false, updating: false, error: null });
    expect(next.postMessage).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it('reports a stale update button instead of silently doing nothing', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    const next = new FakeWorker('installed');
    current.waiting = next;
    const { pwa, reload } = await setup(current, current.active);
    current.waiting = null;
    const beforeReload = vi.fn();
    const beforeActivate = vi.fn();
    const activationFailed = vi.fn();
    await expect(pwa.activatePwaUpdate({ onBeforeReload: beforeReload,
      onBeforeActivate: beforeActivate, onActivationFailed: activationFailed })).resolves.toBe(false);
    expect(pwa.usePwa()).toMatchObject({ updateReady: false, updating: false });
    expect(pwa.usePwa().error).toContain('Готового обновления больше нет');
    expect(next.postMessage).not.toHaveBeenCalled();
    expect(beforeReload).not.toHaveBeenCalled();
    expect(beforeActivate).toHaveBeenCalledOnce();
    expect(activationFailed).toHaveBeenCalledOnce();
    expect(reload).not.toHaveBeenCalled();
  });

  it('resynchronizes a manual update check when the waiting worker has disappeared', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    current.waiting = new FakeWorker('installed');
    const { pwa, reload } = await setup(current, current.active);
    current.update.mockImplementation(async () => { current.waiting = null; return undefined; });
    await pwa.checkPwaUpdate();
    expect(current.update).toHaveBeenCalledOnce();
    expect(pwa.usePwa()).toMatchObject({ updateReady: false, error: null });
    expect(reload).not.toHaveBeenCalled();
  });

  it('keeps the page and drafts when activation times out and allows retry', async () => {
    vi.useFakeTimers();
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    current.waiting = new FakeWorker('installed');
    const { pwa, reload } = await setup(current, current.active);
    const beforeReload = vi.fn();
    const beforeActivate = vi.fn();
    const activationFailed = vi.fn();
    const accepted = pwa.activatePwaUpdate({ onBeforeReload: beforeReload,
      onBeforeActivate: beforeActivate, onActivationFailed: activationFailed });
    await vi.advanceTimersByTimeAsync(15000);
    await expect(accepted).resolves.toBe(false);
    expect(pwa.usePwa()).toMatchObject({ updateReady: true, updating: false });
    expect(pwa.usePwa().error).toContain('данные остаются на экране');
    expect(beforeReload).not.toHaveBeenCalled();
    expect(beforeActivate).toHaveBeenCalledOnce();
    expect(activationFailed).toHaveBeenCalledOnce();
    expect(reload).not.toHaveBeenCalled();
  });

  it('captures the intended view before activation can move layout and samples playback only at reload', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    const next = new FakeWorker('installed');
    current.waiting = next;
    const { pwa, serviceWorker, reload } = await setup(current, current.active);
    let scroll = 800;
    let playback = 12;
    let capturedScroll: number | undefined;
    let savedPlayback: number | undefined;
    const beforeActivate = vi.fn(() => { capturedScroll = scroll; });
    const beforeReload = vi.fn(() => { savedPlayback = playback; });
    next.postMessage.mockImplementation(() => { scroll = 717; });
    const accepted = pwa.activatePwaUpdate({ onBeforeActivate: beforeActivate, onBeforeReload: beforeReload });
    expect(capturedScroll).toBe(800);
    expect(scroll).toBe(717);
    expect(beforeActivate.mock.invocationCallOrder[0]).toBeLessThan(next.postMessage.mock.invocationCallOrder[0]);
    expect(beforeReload).not.toHaveBeenCalled();
    playback = 13;
    current.waiting = null; current.active = next; serviceWorker.controller = next;
    serviceWorker.dispatchEvent(new Event('controllerchange'));
    await expect(accepted).resolves.toBe(true);
    expect(savedPlayback).toBe(13);
    expect(beforeReload.mock.invocationCallOrder[0]).toBeLessThan(reload.mock.invocationCallOrder[0]);
  });

  it('releases captured navigation when sending the activation request fails', async () => {
    const current = new FakeRegistration();
    current.active = new FakeWorker('activated');
    current.waiting = new FakeWorker('installed');
    const { pwa, reload } = await setup(current, current.active);
    current.waiting.postMessage.mockImplementation(() => { throw new Error('worker unavailable'); });
    const beforeActivate = vi.fn(); const activationFailed = vi.fn(); const beforeReload = vi.fn();
    await expect(pwa.activatePwaUpdate({ onBeforeActivate: beforeActivate,
      onActivationFailed: activationFailed, onBeforeReload: beforeReload })).resolves.toBe(false);
    expect(beforeActivate).toHaveBeenCalledOnce();
    expect(activationFailed).toHaveBeenCalledOnce();
    expect(beforeReload).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    expect(pwa.usePwa()).toMatchObject({ updating: false, updateReady: true });
  });
});
