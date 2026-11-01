import { emptyView, parseView } from './viewState';
import type { ViewState } from './viewState';

export const appHistoryKey = 'phonoteka-navigation-v1';
export type HistoryMarker = { token: string; index: number; guard?: boolean };
export type HistoryPort = { push: (marker: HistoryMarker) => boolean; replace: (marker: HistoryMarker) => boolean; go: (delta: number) => boolean };
export type HistoryStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type SavedHistory = { version: 1; token: string; offset: number; cursor: number; entries: ViewState[] };
type Options = {
  port: HistoryPort; store?: HistoryStore; token: string; marker?: unknown; initial: ViewState;
  capture: () => ViewState; apply: (view: ViewState) => void; approve: (view: ViewState) => boolean;
  dismiss: () => boolean; changed: () => void;
};
const limit = 100;
const clean = (view: ViewState) => parseView(JSON.stringify(view));
export const navigationKey = (view: ViewState) => JSON.stringify([view.page, view.query, view.filter, view.pageNumber, view.selectedId, view.page === 'guide' ? view.guideStep || 'folder' : null]);

export function historyMarker(value: unknown): HistoryMarker | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Partial<HistoryMarker>;
  return typeof item.token === 'string' && /^[a-zA-Z0-9-]{8,80}$/.test(item.token)
    && Number.isSafeInteger(item.index) && item.index! >= -1 && item.index! <= 1e6
    && (item.guard === undefined || item.guard === true)
    ? { token: item.token, index: item.index!, ...(item.guard ? { guard: true } : {}) } : null;
}

export function readAppHistory(store?: HistoryStore): SavedHistory | null {
  try {
    const raw = store?.getItem(appHistoryKey);
    if (!raw || raw.length > 256 * 1024) return null;
    const value = JSON.parse(raw) as SavedHistory;
    if (!value || value.version !== 1 || !historyMarker({ token: value.token, index: value.cursor })
      || !Number.isSafeInteger(value.offset) || value.offset < 0 || !Array.isArray(value.entries)
      || !value.entries.length || value.entries.length > limit || value.cursor < value.offset
      || value.cursor >= value.offset + value.entries.length) return null;
    return { version: 1, token: value.token, offset: value.offset, cursor: value.cursor, entries: value.entries.map(clean) };
  } catch { return null; }
}

// Only bounded navigation snapshots are persisted. No server objects, drafts,
// credentials, audio state or job effects are recorded in this history.
export class AppHistory {
  private saved: SavedHistory;
  private native = true;
  private returning: number | null = null;
  private group: string | undefined;

  constructor(private options: Options) {
    const previous = readAppHistory(options.store), marker = historyMarker(options.marker);
    if (previous && marker && !marker.guard && marker.token === previous.token && marker.index === previous.cursor
      && navigationKey(previous.entries[previous.cursor - previous.offset]) === navigationKey(options.initial)) {
      this.saved = previous;
      this.remember(options.initial);
    } else {
      const initial = clean(options.initial);
      const root = { ...initial, page: initial.filter === 'published' ? 'ready' as const : 'library' as const, selectedId: '', scroll: 0, queueScroll: 0 };
      const nested = navigationKey(initial) !== navigationKey(root);
      this.saved = { version: 1, token: options.token, offset: 0, cursor: nested ? 1 : 0, entries: nested ? [root, initial] : [initial] };
      this.native = options.port.replace({ token: this.saved.token, index: -1, guard: true });
      this.native = options.port.push({ token: this.saved.token, index: 0 }) && this.native;
      if (nested) this.native = options.port.push({ token: this.saved.token, index: 1 }) && this.native;
      this.persist();
    }
  }

  get current(): ViewState { return { ...this.saved.entries[this.saved.cursor - this.saved.offset] }; }
  get canBack(): boolean { return this.saved.cursor > this.saved.offset; }
  endGroup() { this.group = undefined; }

  private persist(notify = true) {
    try { this.options.store?.setItem(appHistoryKey, JSON.stringify(this.saved)); } catch { /* Navigation also works in memory. */ }
    if (notify) this.options.changed();
  }

  remember(view: ViewState) {
    this.saved.entries[this.saved.cursor - this.saved.offset] = clean(view);
    this.persist(false);
  }

  push(before: ViewState, next: ViewState, group?: string) {
    this.remember(before);
    if (navigationKey(before) === navigationKey(next)) return;
    const sameSearch = group === 'search' && this.group === group && before.page === next.page
      && before.selectedId === next.selectedId && before.filter === next.filter;
    this.group = group;
    if (sameSearch) {
      this.remember(next);
      return;
    }
    this.saved.entries = this.saved.entries.slice(0, this.saved.cursor - this.saved.offset + 1);
    this.saved.entries.push(clean(next));
    ++this.saved.cursor;
    if (this.saved.entries.length > limit) { this.saved.entries.shift(); ++this.saved.offset; }
    this.native = this.options.port.push({ token: this.saved.token, index: this.saved.cursor }) && this.native;
    this.persist();
  }

  back() {
    if (this.returning !== null || this.options.dismiss()) return;
    if (!this.canBack) return;
    if (!this.native || !this.options.port.go(-1)) { this.remember(this.options.capture()); this.visit(this.saved.cursor - 1); }
  }

  private rebound(target: HistoryMarker) {
    this.returning = this.saved.cursor;
    if (!this.options.port.go(this.saved.cursor - target.index)) this.returning = null;
  }

  private visit(index: number) {
    const target = this.saved.entries[index - this.saved.offset];
    if (!target || !this.options.approve(target)) return false;
    this.saved.cursor = index;
    this.group = undefined;
    this.persist();
    this.options.apply({ ...target });
    return true;
  }

  pop(value: unknown) {
    const target = historyMarker(value);
    if (!target || target.token !== this.saved.token) {
      this.options.port.push({ token: this.saved.token, index: this.saved.cursor });
      return;
    }
    if (this.returning !== null) {
      if (!target.guard && target.index === this.returning) this.returning = null;
      else this.rebound(target);
      return;
    }
    if (!target.guard && target.index === this.saved.cursor) return;
    this.remember(this.options.capture());
    if (this.options.dismiss() || target.guard || target.index < this.saved.offset
      || target.index >= this.saved.offset + this.saved.entries.length || !this.visit(target.index)) this.rebound(target);
  }

  clear() {
    this.saved = { version: 1, token: this.options.token + '-reset', offset: 0, cursor: 0, entries: [{ ...emptyView }] };
    this.returning = null; this.group = undefined;
    try { this.options.store?.removeItem(appHistoryKey); } catch { /* No private navigation snapshot is required to log out. */ }
    this.options.port.replace({ token: this.saved.token, index: -1, guard: true });
    this.options.port.push({ token: this.saved.token, index: 0 });
    this.options.changed();
  }
}
