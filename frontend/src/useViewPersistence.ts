import { useEffect, useLayoutEffect, useRef } from 'react';
import { saveView } from './viewState';
import type { ViewState } from './viewState';

// Only navigation state is stored, never server metadata, credentials or audio.
export function useViewPersistence(view: Omit<ViewState, 'scroll' | 'queueScroll'>, initial: ViewState, ready: boolean, authenticated: boolean) {
  const current = useRef(view); current.current = view;
  const restoring = useRef(true);
  const write = () => {
    if (!authenticated || restoring.current) return;
    saveView({ ...current.current, scroll: window.scrollY, queueScroll: document.querySelector('.queue-tracks')?.scrollTop || 0 });
  };
  const writeRef = useRef(write); writeRef.current = write;
  useEffect(() => { const previous = history.scrollRestoration; history.scrollRestoration = 'manual'; return () => { history.scrollRestoration = previous; }; }, []);
  useLayoutEffect(() => {
    if (!ready || !authenticated || !restoring.current) return;
    const queue = document.querySelector('.queue-tracks');
    const restore = () => { window.scrollTo({ top: initial.scroll, behavior: 'instant' }); if (queue) queue.scrollTop = initial.queueScroll; };
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
  }, [ready, authenticated, initial]);
  useEffect(() => { writeRef.current(); }, [view.page, view.query, view.filter, view.pageNumber, view.selectedId]);
  useEffect(() => {
    const remember = () => writeRef.current();
    window.addEventListener('scroll', remember, { passive: true, capture: true });
    window.addEventListener('pagehide', remember);
    return () => { window.removeEventListener('scroll', remember, true); window.removeEventListener('pagehide', remember); };
  }, []);
  return () => writeRef.current();
}
