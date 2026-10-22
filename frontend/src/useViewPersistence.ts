import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { saveView } from './viewState';
import type { ViewState } from './viewState';

// Only navigation state is stored, never server metadata, credentials or audio.
export function useViewPersistence(view: Omit<ViewState, 'scroll' | 'queueScroll'>, initial: ViewState, ready: boolean, authenticated: boolean) {
  const current = useRef(view); current.current = view;
  const restoring = useRef(true);
  const target = useRef(initial);
  const [restoreVersion, setRestoreVersion] = useState(0);
  const reloadView = useRef<ViewState | null>(null);
  const capture = (): ViewState => reloadView.current ?? { ...current.current,
    scroll: restoring.current ? target.current.scroll : window.scrollY,
    queueScroll: restoring.current ? target.current.queueScroll : document.querySelector('.queue-tracks')?.scrollTop || 0 };
  const write = () => {
    if (!authenticated) return;
    // Activation can change page layout before controllerchange/pagehide.
    // Keep the owner's captured view throughout that transition.
    if (reloadView.current) { saveView(reloadView.current); return; }
    if (restoring.current) return;
    saveView({ ...current.current, scroll: window.scrollY, queueScroll: document.querySelector('.queue-tracks')?.scrollTop || 0 });
  };
  const writeRef = useRef(write); writeRef.current = write;
  useEffect(() => { const previous = history.scrollRestoration; history.scrollRestoration = 'manual'; return () => { history.scrollRestoration = previous; }; }, []);
  useLayoutEffect(() => {
    if (!ready || !authenticated || !restoring.current) return;
    const queue = document.querySelector('.queue-tracks');
    const restore = () => { window.scrollTo({ top: target.current.scroll, behavior: 'instant' }); if (queue) queue.scrollTop = target.current.queueScroll; };
    restore();
    const finish = () => { restoring.current = false; observer.disconnect(); window.clearTimeout(timer); writeRef.current(); };
    const observer = new ResizeObserver(restore);
    const main = document.getElementById('main-content'); if (main) observer.observe(main);
    const timer = window.setTimeout(finish, 1500);
    const userAction = () => finish();
    window.addEventListener('pointerdown', userAction, { once: true });
    window.addEventListener('wheel', userAction, { once: true, passive: true });
    window.addEventListener('keydown', userAction, { once: true });
    return () => { observer.disconnect(); clearTimeout(timer); window.removeEventListener('pointerdown', userAction); window.removeEventListener('wheel', userAction); window.removeEventListener('keydown', userAction); };
  }, [ready, authenticated, restoreVersion]);
  useEffect(() => { writeRef.current(); }, [view.page, view.query, view.filter, view.pageNumber, view.selectedId, view.guideStep]);
  useEffect(() => {
    const remember = () => writeRef.current();
    window.addEventListener('scroll', remember, { passive: true, capture: true });
    window.addEventListener('pagehide', remember);
    return () => { window.removeEventListener('scroll', remember, true); window.removeEventListener('pagehide', remember); };
  }, []);
  return Object.assign(() => writeRef.current(), {
    capture,
    restore: (next: ViewState) => { target.current = next; restoring.current = true; setRestoreVersion(value => value + 1); },
    freezeForReload: () => {
      if (!authenticated || reloadView.current) return;
      reloadView.current = capture();
      saveView(reloadView.current);
    },
    releaseReload: () => { reloadView.current = null; writeRef.current(); },
  });
}
