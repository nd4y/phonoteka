import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ViewState } from './viewState';
import { readView } from './viewState';
import { useViewPersistence } from './useViewPersistence';

type Effect = () => void | (() => void);
const { effects, layouts } = vi.hoisted(() => ({ effects: [] as Effect[], layouts: [] as Effect[] }));
vi.mock('react', () => ({
  useRef: <T>(value: T) => ({ current: value }),
  useState: <T>(value: T) => [value, vi.fn()],
  useEffect: (effect: Effect) => { effects.push(effect); },
  useLayoutEffect: (effect: Effect) => { layouts.push(effect); },
}));

const cleanups: Array<() => void> = [];
function setup(authenticated = true, finishRestoration = true) {
  vi.useFakeTimers(); effects.length = 0; layouts.length = 0;
  const values = new Map<string, string>();
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); } });
  const queue = { scrollTop: 0 };
  const target = Object.assign(new EventTarget(), { scrollY: 0,
    scrollTo: vi.fn(), setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout });
  target.scrollTo.mockImplementation(({ top }: { top: number }) => { target.scrollY = top; });
  vi.stubGlobal('window', target);
  vi.stubGlobal('history', { scrollRestoration: 'auto' });
  vi.stubGlobal('document', { querySelector: () => queue, getElementById: () => ({}) });
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const initial: ViewState = { page: 'ready', query: '002', filter: 'published', pageNumber: 2,
    selectedId: 'fixture-002', scroll: 800, queueScroll: 140 };
  const persistence = useViewPersistence(initial, initial, true, authenticated);
  for (const effect of [...layouts, ...effects]) {
    const cleanup = effect(); if (cleanup) cleanups.push(cleanup);
  }
  if (finishRestoration) target.dispatchEvent(new Event('wheel'));
  return { target, queue, persistence, initial, values };
}

afterEach(() => {
  cleanups.splice(0).reverse().forEach(cleanup => cleanup());
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
});

describe('view persistence during an explicit PWA activation', () => {
  it('persists an immediate F5 destination before Back content restoration finishes', () => {
    const { target, queue, persistence, initial } = setup();
    const destination: ViewState = { ...initial, page:'guide',guideStep:'recognize',scroll:420,queueScroll:0 };
    persistence.restore(destination);
    target.scrollY=0; queue.scrollTop=0;
    target.dispatchEvent(new Event('pagehide'));
    expect(readView()).toEqual(destination);
  });
  it('captures the destination offsets while a Back restoration waits for content', () => {
    const { target, queue, persistence, initial } = setup();
    const destination = { ...initial, scroll: 1240, queueScroll: 260 };
    persistence.restore(destination);
    target.scrollY = 0; queue.scrollTop = 0;
    expect(persistence.capture()).toMatchObject({ scroll: 1240, queueScroll: 260 });
    persistence.freezeForReload(); target.dispatchEvent(new Event('pagehide'));
    expect(readView()).toMatchObject({ scroll: 1240, queueScroll: 260 });
  });

  it('applies a later history restoration instead of the original startup offsets', () => {
    const { target, queue, persistence, initial } = setup();
    persistence.restore({ ...initial, scroll: 640, queueScroll: 70 });
    target.scrollY = 0; queue.scrollTop = 0;
    const cleanup = layouts[0](); if (cleanup) cleanups.push(cleanup);
    expect(target.scrollY).toBe(640); expect(queue.scrollTop).toBe(70);
    target.dispatchEvent(new Event('wheel'));
    expect(readView()).toMatchObject({ scroll: 640, queueScroll: 70 });
  });

  it('keeps the captured view through banner-removal scroll and pagehide before reload', () => {
    const { target, queue, persistence, initial } = setup();
    persistence.freezeForReload();
    target.scrollY = 717; queue.scrollTop = 0;
    target.dispatchEvent(new Event('scroll'));
    expect(readView()).toEqual(initial);
    target.scrollY = 0;
    target.dispatchEvent(new Event('pagehide'));
    persistence();
    expect(readView()).toEqual(initial);
  });

  it('resumes normal scroll and pagehide persistence when activation fails', () => {
    const { target, queue, persistence, initial } = setup();
    persistence.freezeForReload();
    target.scrollY = 717; queue.scrollTop = 100;
    target.dispatchEvent(new Event('scroll'));
    expect(readView()).toEqual(initial);
    persistence.releaseReload();
    expect(readView()).toEqual({ ...initial, scroll: 717, queueScroll: 100 });
    target.scrollY = 900; queue.scrollTop = 180;
    target.dispatchEvent(new Event('pagehide'));
    expect(readView()).toEqual({ ...initial, scroll: 900, queueScroll: 180 });
  });

  it('captures intended restoration offsets if the owner accepts while content is still restoring', () => {
    const { target, queue, persistence, initial } = setup(true, false);
    target.scrollY = 0; queue.scrollTop = 0;
    persistence.freezeForReload();
    target.dispatchEvent(new Event('pagehide'));
    expect(readView()).toEqual(initial);
  });

  it('does not store a navigation snapshot for an unauthenticated page', () => {
    const { persistence, values, target } = setup(false);
    persistence.freezeForReload(); persistence();
    target.dispatchEvent(new Event('pagehide'));
    expect(values.size).toBe(0);
  });
});
