import { describe, expect, it, vi } from 'vitest';
import { AppHistory, appHistoryKey, historyMarker, readAppHistory } from './appHistory';
import type { HistoryMarker } from './appHistory';
import { emptyView } from './viewState';
import type { ViewState } from './viewState';
import { acceptDetailNavigation, loadLatestResult } from './detailNavigation';

function fixture(initial = { ...emptyView }, available = true) {
  const values = new Map<string, string>();
  const store = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  const stack: HistoryMarker[] = [];
  let position = -1, pending: number | null = null, view = initial;
  const apply = vi.fn((next: ViewState) => { view = next; });
  const approve = vi.fn(() => true), dismiss = vi.fn(() => false);
  const port = {
    replace: (marker: HistoryMarker) => { if (!available) return false; stack[Math.max(0, position)] = marker; position = Math.max(0, position); return true; },
    push: (marker: HistoryMarker) => { if (!available) return false; stack.splice(position + 1); stack.push(marker); ++position; return true; },
    go: (delta: number) => { if (!available) return false; pending = position + delta; return true; },
  };
  const options = { port, store, token: 'test-session-token', initial, capture: () => view, apply, approve, dismiss, changed: vi.fn() };
  let controller = new AppHistory(options);
  function flush() {
    let remaining = 8;
    while (pending !== null && --remaining) {
      const target = pending; pending = null;
      if (target < 0 || target >= stack.length) throw Error('navigation escaped application boundary');
      position = target; controller.pop(stack[position]);
    }
    if (!remaining) throw Error('history rebound loop');
  }
  return { store, stack, apply, approve, dismiss, get controller() { return controller; }, get view() { return view; },
    setView: (next: ViewState) => { view = next; },
    push: (next: Partial<ViewState>, group?: string) => { const target = { ...view, ...next }; controller.push(view, target, group); view = target; },
    back: () => { controller.back(); flush(); }, forward: () => { port.go(1); flush(); },
    nativeBack: () => { port.go(-1); flush(); },
    reload: () => { controller = new AppHistory({ ...options, initial: view, marker: stack[position] }); },
  };
}

describe('application Back navigation', () => {
  it('a newer catalog action cancels pending details without rewriting that action in history', async () => {
    const f = fixture(), sequence = { current: 0 };
    let resolve!: (track: string) => void;
    const response = new Promise<string>(yes => { resolve = yes; });
    const pending = loadLatestResult(sequence, () => response,
      id => f.push({ page: 'review', selectedId: id }), vi.fn(), vi.fn());
    acceptDetailNavigation(sequence, () => true);
    f.push({ filter: 'missing_lyrics', pageNumber: 2 });
    resolve('late-track'); expect(await pending).toBe(false);
    expect(f.view).toMatchObject({ page: 'library', filter: 'missing_lyrics', pageNumber: 2, selectedId: '' });
    f.back(); expect(f.view).toEqual(emptyView);
    f.forward(); expect(f.view).toMatchObject({ filter: 'missing_lyrics', pageNumber: 2 });
  });

  it('returns through a selected track, filters and the collection without replacing playback', () => {
    const f = fixture({ ...emptyView, scroll: 480, queueScroll: 120 });
    f.push({ filter: 'missing_lyrics', pageNumber: 3, scroll: 620 });
    f.push({ page: 'review', selectedId: 'track-a', scroll: 15 });
    f.push({ page: 'jobs', selectedId: '', scroll: 0 });
    f.back(); expect(f.view).toMatchObject({ page: 'review', selectedId: 'track-a', scroll: 15 });
    f.back(); expect(f.view).toMatchObject({ page: 'library', filter: 'missing_lyrics', pageNumber: 3, scroll: 620 });
    f.back(); expect(f.view).toMatchObject({ ...emptyView, scroll: 480, queueScroll: 120 });
    expect(f.apply).toHaveBeenCalledTimes(3);
    expect(f.controller.canBack).toBe(false);
  });

  it('browser Back at the first screen remains inside the app', () => {
    const f = fixture(); f.nativeBack();
    expect(f.view).toEqual(emptyView); expect(f.apply).not.toHaveBeenCalled();
    expect(f.stack).toHaveLength(2);
  });

  it('closes a transient panel before consuming a navigation step', () => {
    const f = fixture(); f.push({ page: 'settings' });
    f.dismiss.mockReturnValueOnce(true); f.nativeBack();
    expect(f.view.page).toBe('settings'); expect(f.apply).not.toHaveBeenCalled();
    f.nativeBack(); expect(f.view.page).toBe('library');
  });

  it('the in-app Back button also dismisses an overlay first', () => {
    const f = fixture(); f.push({ page: 'review', selectedId: 'cue-sheet' });
    f.dismiss.mockReturnValueOnce(true); f.back();
    expect(f.view.selectedId).toBe('cue-sheet'); expect(f.apply).not.toHaveBeenCalled();
    f.back(); expect(f.view.page).toBe('library');
  });

  it('rejecting draft loss restores the browser cursor and allows a later accepted Back', () => {
    const f = fixture(); f.push({ page: 'review', selectedId: 'draft-track' });
    f.approve.mockReturnValueOnce(false); f.nativeBack();
    expect(f.view.selectedId).toBe('draft-track'); expect(f.apply).not.toHaveBeenCalled();
    expect(f.controller.canBack).toBe(true);
    f.nativeBack(); expect(f.view.page).toBe('library');
    f.forward(); expect(f.view.selectedId).toBe('draft-track');
  });

  it('Forward restores the stored selection and truncates when a new route branches', () => {
    const f = fixture(); f.push({ page: 'review', selectedId: 'a' }); f.push({ page: 'lyrics' });
    f.back(); f.forward(); expect(f.view).toMatchObject({ page: 'lyrics', selectedId: 'a' });
    f.back(); f.push({ page: 'reports', selectedId: '' }); f.back(); f.forward();
    expect(f.view.page).toBe('reports'); expect(f.stack).toHaveLength(4);
  });

  it('coalesces typing into one step and keeps a later search as another step', () => {
    const f = fixture(); f.push({ query: 'm' }, 'search'); f.push({ query: 'mu' }, 'search'); f.push({ query: 'music' }, 'search');
    f.controller.endGroup(); f.push({ query: 'other' }, 'search');
    f.back(); expect(f.view.query).toBe('music'); f.back(); expect(f.view.query).toBe('');
  });

  it('refreshing data without a navigation change does not create a step', () => {
    const f = fixture(); f.push({ scroll: 200 }); f.back();
    expect(f.controller.canBack).toBe(false); expect(f.stack).toHaveLength(2);
  });

  it('reload resumes the same native cursor without adding entries', () => {
    const f = fixture(); f.push({ page: 'review', selectedId: 'a', scroll: 520 }); f.push({ page: 'lyrics' });
    const length = f.stack.length; f.reload();
    expect(f.stack).toHaveLength(length); f.back();
    expect(f.view).toMatchObject({ page: 'review', selectedId: 'a', scroll: 520 });
  });

  it('a restored nested editor has a collection fallback', () => {
    const f = fixture({ ...emptyView, page: 'lyrics', selectedId: 'restored' });
    f.back(); expect(f.view).toMatchObject({ page: 'library', selectedId: '' });
  });

  it('continues in memory if native history and storage are unavailable', () => {
    const f = fixture({ ...emptyView }, false); f.push({ page: 'jobs' }); f.back();
    expect(f.view.page).toBe('library');
  });

  it('bounds stored history and prevents crossing its oldest retained frame', () => {
    const f = fixture(); for (let i = 1; i <= 105; ++i) f.push({ pageNumber: i + 1 });
    expect(readAppHistory(f.store)?.entries).toHaveLength(100);
    for (let i = 0; i < 99; ++i) f.back();
    expect(f.view.pageNumber).toBe(7); f.nativeBack(); expect(f.view.pageNumber).toBe(7);
  });

  it('logout discards private snapshots and old browser entries cannot reopen them', () => {
    const f = fixture(); f.push({ page: 'review', selectedId: 'private-track', query: 'private-query' });
    f.controller.clear(); f.setView({ ...emptyView });
    expect(f.store.getItem(appHistoryKey)).toBeNull(); f.nativeBack();
    expect(f.apply).not.toHaveBeenCalled(); expect(f.view).toEqual(emptyView);
  });

  it('rejects malformed and oversized persisted state', () => {
    const f = fixture(); f.store.setItem(appHistoryKey, '{'); expect(readAppHistory(f.store)).toBeNull();
    f.store.setItem(appHistoryKey, 'x'.repeat(256 * 1024 + 1)); expect(readAppHistory(f.store)).toBeNull();
    expect(historyMarker({ token: 'short', index: 0 })).toBeNull();
    expect(historyMarker({ token: 'test-session-token', index: -2 })).toBeNull();
  });
});
