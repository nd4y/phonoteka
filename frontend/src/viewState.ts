import { guideSteps } from './guideState';
import type { GuideStep } from './guideState';
export type Page = 'library' | 'review' | 'lyrics' | 'jobs' | 'ready' | 'settings' | 'reports' | 'guide' | 'help';
export type ViewState = { page: Page; query: string; filter: string; pageNumber: number; selectedId: string; scroll: number; queueScroll: number; guideStep?: GuideStep };
const pages: Page[] = ['library', 'review', 'lyrics', 'jobs', 'ready', 'settings', 'reports', 'guide', 'help'];
const filters = ['all', 'cue', 'review', 'missing_lyrics', 'missing_art', 'published', 'audit_errors', 'audit_warnings', 'audit_unchecked', 'audit_passed'];
export const viewKey = 'phonoteka-view-v1';
export const emptyView: ViewState = { page: 'library', query: '', filter: 'all', pageNumber: 1, selectedId: '', scroll: 0, queueScroll: 0 };
export function parseView(raw: string | null): ViewState {
  try {
    const value = JSON.parse(raw || '{}');
    const position = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.min(v, 1e7)) : 0;
    return {
      page: pages.includes(value.page) ? value.page : 'library',
      query: typeof value.query === 'string' ? value.query.slice(0, 200) : '',
      filter: filters.includes(value.filter) ? value.filter : 'all',
      pageNumber: Number.isSafeInteger(value.pageNumber) && value.pageNumber > 0 ? Math.min(value.pageNumber, 100000) : 1,
      selectedId: typeof value.selectedId === 'string' ? value.selectedId.slice(0, 200) : '',
      scroll: position(value.scroll), queueScroll: position(value.queueScroll),
      ...(guideSteps.includes(value.guideStep) ? { guideStep: value.guideStep } : {}),
    };
  } catch { return { ...emptyView }; }
}
export function readView(): ViewState {
  try { return parseView(sessionStorage.getItem(viewKey)); } catch { return { ...emptyView }; }
}
export function saveView(view: ViewState) {
  try { sessionStorage.setItem(viewKey, JSON.stringify(view)); } catch { /* A disabled browser store must not prevent navigation. */ }
}
export function clearView() {
  try { sessionStorage.removeItem(viewKey); } catch { /* Logging out also works without browser storage. */ }
}
