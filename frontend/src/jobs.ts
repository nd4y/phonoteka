import type { Job } from './types';

export type LiveJob = Job & {
  processed?: number; remaining?: number; current_action?: string; active_count?: number;
  eta_seconds?: number | null; eta_reason?: string; recent_rate?: number | null;
};
export type ActivityEvent = {
  id: number; kind: string; created_at: string; message: string; track_id?: string;
  filename?: string; state?: string; completed?: number; failed?: number; retry_at?: number; code?: string;
  provider?: string; duration_ms?: number; http_status?: number;
};
export type ProcessingStage = {
  stage_id: string; attempt_id: string; track_id: string; filename?: string | null; stage: string; provider: string | null;
  state: string; duration_ms: number | null; elapsed_seconds: number | null;
  http_status: number | null; reason_code: string | null; long_running: boolean;
  progress_counter?: number; progress_unit?: string; last_progress_age_seconds?: number | null; no_progress_suspected?: boolean;
};
export type JobActivity = {
  job: LiveJob; active_items: Array<{ track_id: string | null; filename: string; action: string; started_at?: string | null }>;
  active_items_truncated: boolean; events: ActivityEvent[]; cursor: number; resync_required: boolean; has_more: boolean;
  server_now?: number; executor_health?: string; heartbeat_age_seconds?: number | null;
  current_stages?: ProcessingStage[]; stage_history?: ProcessingStage[]; stages_truncated?: boolean;
  provider_waits?: Array<{track_id: string | null; filename: string; provider: string; reason_code: string; retry_at: number | null; overdue_wait: boolean}>;
  provider_waits_truncated?: boolean;
};
export const jobNames: Record<string, string> = {
  publish_sidecars: 'Публикация LRC и обложек', release_audit: 'Сверка релиза', rollback_ready: 'Откат опубликованной версии',
  youtube: 'Связи с YouTube', normalize_audio: 'Подготовка редактируемой копии', replaygain_album: 'Громкость альбома',
  export_ready: 'Экспорт готовой коллекции', extract_cue: 'Извлечение CUE', replaygain: 'Измерение ReplayGain',
  recognize: 'Распознавание аудиоотпечатков', transcribe: 'Проверка содержимого ASR', align: 'Сопоставление текста с аудио',
  analyze: 'Поиск метаданных и текстов', scan: 'Сканирование', audit: 'Проверка аудио', apply: 'Запись изменений',
  rollback: 'Откат изменений', publish: 'Публикация',
};
export const jobStates: Record<string, string> = {
  waiting_authorization: 'Нужно разрешение владельца',
  waiting_storage: 'Ожидание места',
  paused: 'Приостановлено', waiting_provider: 'Ожидание сервиса', deferred: 'Ожидает повтора', queued: 'В очереди',
  running: 'В работе', completed: 'Завершено', completed_with_errors: 'Завершено с ошибками', succeeded: 'Завершено',
  failed: 'Ошибка', canceled: 'Отменено', cancelled: 'Отменено', cancelled_partial: 'Отменено частично',
  interrupted: 'Прервано', needs_review: 'Нужно решение', pending: 'Ожидание', conflict: 'Конфликт версий',
};
export const isActiveJob = (job: Job) => ['queued', 'running', 'pending', 'paused', 'waiting_provider', 'waiting_storage', 'waiting_authorization', 'deferred'].includes(job.state);
export const needsJobAuthorization = (job: Job) => job.state === 'waiting_authorization' || job.state === 'running' && job.stage === 'waiting_authorization';
export function jobTone(job: Job): 'success' | 'cancelled' | 'error' | 'active' | 'waiting' | 'paused' | 'authorization' {
  if (['canceled', 'cancelled', 'cancelled_partial'].includes(job.state) || job.cancel_requested && isActiveJob(job)) return 'cancelled';
  if (needsJobAuthorization(job)) return 'authorization';
  if (['failed', 'interrupted', 'completed_with_errors', 'needs_review', 'conflict'].includes(job.state)) return 'error';
  if (['succeeded', 'completed'].includes(job.state)) return job.failed ? 'error' : 'success';
  if (job.state === 'paused' || job.pause_requested) return 'paused';
  if (['waiting_provider','waiting_storage'].includes(job.stage) || ['queued', 'waiting_provider', 'deferred', 'pending'].includes(job.state)) return 'waiting';
  if (job.state === 'running') return 'active';
  if (job.failed) return 'error';
  return 'success';
}
export function etaText(job: LiveJob): string {
  if (job.cancel_requested && isActiveJob(job)) return 'Завершаем текущие операции';
  if (needsJobAuthorization(job)) return 'ETA после подтверждения владельца';
  const seconds = job.eta_seconds;
  if (typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0) {
    const duration = seconds < 60 ? `${Math.ceil(seconds)} с` : seconds < 3600 ? `${Math.ceil(seconds / 60)} мин`
      : `${Math.floor(seconds / 3600)} ч ${Math.ceil(seconds % 3600 / 60)} мин`;
    return `Осталось около ${duration}`;
  }
  return ({ queued: 'ETA после запуска', paused: 'ETA на паузе', waiting_authorization: 'ETA после подтверждения владельца', waiting_storage: 'ETA после освобождения места', waiting_provider: 'ETA после ответа сервиса',
    cancelling: 'Завершаем текущие операции', unknown_total: 'ETA после определения объема', finalizing: 'Завершающие действия',
    no_recent_progress: 'ETA уточняется: нет свежего прогресса', finished: 'Обработка завершена',
    insufficient_samples: 'Собираем данные для ETA' } as Record<string, string>)[job.eta_reason || 'insufficient_samples'] || 'ETA пока не определено';
}
export function mergeActivityEvents(previous: ActivityEvent[], incoming: ActivityEvent[], limit = 200): ActivityEvent[] {
  const events = new Map<number, ActivityEvent>();
  for (const event of [...previous, ...incoming]) {
    if (Number.isSafeInteger(event.id) && event.id > 0 && typeof event.message === 'string') events.set(event.id, event);
  }
  return [...events.values()].sort((a, b) => a.id - b.id).slice(-Math.max(1, limit));
}
