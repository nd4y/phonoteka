import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { AppHistory } from './appHistory';
import type { HistoryMarker, HistoryStore } from './appHistory';
import type { ViewState } from './viewState';

type BackLayers = { register: (dismiss: () => void, priority: number) => () => void };
export const AppBackContext = createContext<BackLayers | null>(null);
export function useBackLayer(enabled: boolean, dismiss: () => void, priority = 0, supplied?: BackLayers) {
  const context = useContext(AppBackContext), latest = useRef(dismiss); latest.current = dismiss;
  const layers = supplied ?? context;
  useEffect(() => enabled && layers ? layers.register(() => latest.current(), priority) : undefined, [enabled, layers, priority]);
}

export function useAppHistory(options: { authenticated: boolean; view: Omit<ViewState, 'scroll' | 'queueScroll'>;
  capture: () => ViewState; apply: (view: ViewState) => void; approve: (view: ViewState) => boolean }) {
  const callbacks = useRef(options); callbacks.current = options;
  const engine = useRef<AppHistory | null>(null);
  const closers = useRef(new Map<symbol, { dismiss: () => void; priority: number }>());
  const disclosures = useRef<HTMLDetailsElement[]>([]);
  const [, update] = useState(0);
  const changed = () => update(value => value + 1);
  const layers = useRef<BackLayers>({ register(dismiss, priority) {
    const id = Symbol(); closers.current.set(id, { dismiss, priority }); changed();
    return () => { closers.current.delete(id); changed(); };
  } }).current;
  function dismiss() {
    const active = [...closers.current.values()].reverse().sort((a, b) => b.priority - a.priority)[0];
    if (active && active.priority > 0) { active.dismiss(); return true; }
    disclosures.current = disclosures.current.filter(element => element.isConnected && element.open);
    const last = disclosures.current.pop();
    if (last) { last.open = false; changed(); return true; }
    if (active) { active.dismiss(); return true; }
    return false;
  }
  const dismissRef = useRef(dismiss); dismissRef.current = dismiss;
  useEffect(() => {
    if (!options.authenticated) { engine.current?.clear(); engine.current = null; return; }
    let store: HistoryStore | undefined;
    try { store = sessionStorage; } catch { /* Browser storage may be disabled. */ }
    const write = (marker: HistoryMarker, push: boolean) => {
      try {
        const previous = history.state && typeof history.state === 'object' ? history.state : {};
        history[push ? 'pushState' : 'replaceState']({ ...previous, phonotekaNavigation: marker }, '', location.href);
        return true;
      } catch { return false; }
    };
    engine.current = new AppHistory({ port: { push: marker => write(marker, true), replace: marker => write(marker, false), go: delta => { try { history.go(delta); return true; } catch { return false; } } },
      token: crypto.randomUUID(), store, marker: history.state?.phonotekaNavigation, initial: callbacks.current.capture(),
      capture: () => callbacks.current.capture(), apply: view => callbacks.current.apply(view), approve: view => callbacks.current.approve(view),
      dismiss: () => dismissRef.current(), changed });
    changed();
    const pop = (event: PopStateEvent) => engine.current?.pop(event.state?.phonotekaNavigation);
    const remember = () => engine.current?.remember(callbacks.current.capture());
    const blur = (event: FocusEvent) => { if (event.target instanceof HTMLInputElement && event.target.closest('.search-field')) engine.current?.endGroup(); };
    const toggle = (event: Event) => {
      const element = event.target;
      if (!(element instanceof HTMLDetailsElement) || !element.closest('.app-shell')) return;
      disclosures.current = disclosures.current.filter(item => item !== element && item.isConnected && item.open);
      if (element.open) disclosures.current.push(element);
      changed();
    };
    const keyboard = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.key === 'ArrowLeft') { event.preventDefault(); engine.current?.back(); }
      else if (event.key === 'Escape' && !document.querySelector('dialog[open]') && dismissRef.current()) event.preventDefault();
    };
    window.addEventListener('popstate', pop); window.addEventListener('pagehide', remember);
    window.addEventListener('scroll', remember, { passive: true, capture: true });
    window.addEventListener('keydown', keyboard); document.addEventListener('toggle', toggle, true);
    document.addEventListener('focusout', blur);
    return () => { window.removeEventListener('popstate', pop); window.removeEventListener('pagehide', remember);
      window.removeEventListener('scroll', remember, true); window.removeEventListener('keydown', keyboard); document.removeEventListener('toggle', toggle, true); document.removeEventListener('focusout', blur); };
  }, [options.authenticated]);
  useLayoutEffect(() => { engine.current?.remember(callbacks.current.capture()); }, [options.view.page, options.view.query, options.view.filter, options.view.pageNumber, options.view.selectedId]);
  return { layers, canBack: !!engine.current?.canBack || !!closers.current.size || disclosures.current.some(element => element.isConnected && element.open),
    back: () => engine.current?.back(), endGroup: () => engine.current?.endGroup(),
    push: (next: ViewState, group?: string, before = callbacks.current.capture()) => engine.current?.push(before, next, group),
    clear: () => { engine.current?.clear(); disclosures.current = []; } };
}
