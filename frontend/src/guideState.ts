import type { Job } from './types';

export const guideSteps = ['folder', 'scan', 'recognize', 'metadata', 'review', 'ready'] as const;
export type GuideStep = typeof guideSteps[number];
export type GuideState = { step: GuideStep; history: GuideStep[] };
export const guideStorageKey = 'phonoteka-guide-v1';
const validStep = (value: unknown): value is GuideStep => guideSteps.includes(value as GuideStep);
export function parseGuideState(raw: string | null): GuideState {
  try {
    const value = JSON.parse(raw || '{}');
    return { step: validStep(value.step) ? value.step : 'folder',
      history: Array.isArray(value.history) ? value.history.filter(validStep).slice(-20) : [] };
  } catch { return { step: 'folder', history: [] }; }
}
export function moveGuide(state: GuideState, step: GuideStep): GuideState {
  return step === state.step ? state : { step, history: [...state.history, state.step].slice(-20) };
}
export function backGuide(state: GuideState): GuideState {
  return state.history.length ? { step: state.history[state.history.length - 1], history: state.history.slice(0, -1) } : state;
}
export function readGuideState(): GuideState {
  try { return parseGuideState(sessionStorage.getItem(guideStorageKey)); } catch { return parseGuideState(null); }
}
export function saveGuideState(value: GuideState) {
  try { sessionStorage.setItem(guideStorageKey, JSON.stringify(value)); } catch { /* Guidance also works without storage. */ }
}
export function clearGuideState() {
  try { sessionStorage.removeItem(guideStorageKey); } catch { /* Logout must not depend on storage. */ }
}
export const guideJobKind = (step: GuideStep) => ({ scan: 'scan', recognize: 'recognize', metadata: 'analyze' } as Partial<Record<GuideStep, string>>)[step];
export function recommendedGuideStep(counts: Record<string, number> | null): GuideStep {
  if (!counts) return 'folder';
  if (counts.audio_files === 0) return 'scan';
  if (counts.current_proposals > 0) return 'review';
  if (counts.metadata_pending > 0 && counts.with_core_tags > 0) return 'metadata';
  if (counts.recognition_pending > 0) return 'recognize';
  if (counts.metadata_pending > 0) return 'metadata';
  if (counts.audio_files > 0 && counts.published_current === counts.audio_files) return 'ready';
  return 'review';
}
export function activeGuideJob(jobs: Job[], step: GuideStep) {
  const active = ['queued', 'running', 'pending', 'paused', 'waiting_provider', 'waiting_authorization', 'deferred'];
  return jobs.find(job => job.kind === guideJobKind(step) && active.includes(job.state));
}
