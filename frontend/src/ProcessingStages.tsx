import { useEffect, useState } from 'react';
import type { JobActivity } from './jobs';

const labels: Record<string, string> = {
  file: 'Попытка обработки', preflight: 'Проверка файла', decode: 'Декодирование', write_tags: 'Запись тегов', commit: 'Проверка и фиксация',
  metadata_lookup: 'Метаданные и текст', fingerprint_sample: 'Аудиоотпечаток', fingerprint_lookup: 'Распознавание',
  provider_lookup: 'Обращение к источнику', provider_admission: 'Допуск к сервису', provider_http: 'HTTP-запрос',
  asr: 'Локальный ASR', align: 'Синхронизация', rollback: 'Откат', publication: 'Публикация', normalize: 'Преобразование', cue_extract: 'Извлечение CUE',
};
const reasons: Record<string, string> = { provider_unavailable: 'Сервис недоступен', provider_cooldown: 'Пауза после ошибок сервиса', rate_limited: 'Ограничение частоты запросов', budget_exhausted: 'Лимит запросов исчерпан' };
const states: Record<string, string> = { succeeded: 'Завершен', failed: 'Ошибка', interrupted: 'Прерван или конец не измерен', deferred: 'Отложен', cancelled: 'Отменен', running: 'Выполняется', conflict: 'Конфликт', needs_review: 'Нужна проверка' };
function duration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return 'не измерено';
  if (seconds < 1) return `${Math.round(seconds * 1000)} мс`;
  return `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
}

export function ProcessingStages({ activity }: { activity: JobActivity }) {
  const [clock, setClock] = useState({ received: Date.now(), now: Date.now() });
  const [longOnly, setLongOnly] = useState(false);
  const [stalledOnly, setStalledOnly] = useState(false);
  useEffect(() => { const now = Date.now(); setClock({received: now, now}); }, [activity]);
  useEffect(() => {
    const timer = setInterval(() => setClock(value => ({ ...value, now: Date.now() })), 1000);
    return () => clearInterval(timer);
  }, []);
  const age = Math.max(0, (clock.now - clock.received) / 1000);
  const stale = age > 15;
  const stages = activity.current_stages || [];
  const history = activity.stage_history || [];
  const waits = activity.provider_waits || [];
  if (!activity.server_now) return null;
  return <section className="processing-stages" aria-label="Этапы обработки">
    <h4>Ход обработки</h4>
    {stale && <p role="status">Данные устарели. Состояние исполнителя неизвестно.</p>}
    {!stale && activity.job.state === 'running' && activity.executor_health === 'executor_unavailable' && <p role="status">Нет ответа исполнителя более минуты.</p>}
    {!stale && activity.job.state === 'running' && activity.executor_health === 'heartbeat_stale' && <p role="status">Нет свежего ответа исполнителя.</p>}
    <p className="muted small">Внутренний прогресс атомарных операций не измеряется. Ответ исполнителя не подтверждает продвижение файла.</p>
    <label className="small"><input type="checkbox" checked={longOnly} onChange={event => setLongOnly(event.target.checked)} /> Только долгие этапы</label>
    <label className="small"><input type="checkbox" checked={stalledOnly} onChange={event => setStalledOnly(event.target.checked)} /> Возможное отсутствие прогресса</label>
    {stages.filter(stage => (!longOnly || stage.long_running) && (!stalledOnly || stage.no_progress_suspected)).map(stage => <div className="processing-stage" key={stage.stage_id}>
      <strong>{stage.filename || 'Файл'} · {labels[stage.stage] || stage.stage}</strong>
      <span>{stage.provider || 'Локальная обработка'} · {duration(stage.elapsed_seconds === null ? null : stage.elapsed_seconds + (stale ? 0 : age))}{stale ? ' · На момент обновления' : stage.long_running ? ' · Дольше ожидаемого' : ''}</span>
      {stage.progress_counter !== undefined && <small>Измеренный прогресс: {stage.progress_counter} {stage.progress_unit === 'bytes' ? 'байт' : stage.progress_unit === 'responses' ? 'ответов' : 'единиц'}.{stage.last_progress_age_seconds != null && !stale ? ` Последнее изменение ${duration(stage.last_progress_age_seconds + age)} назад.` : ''}</small>}
      {!stale && stage.no_progress_suspected && <small>Возможно, нет прогресса: измеряемый счетчик не изменялся более 120 секунд при свежем ответе исполнителя.</small>}
    </div>)}
    {!stages.length && !waits.length && <p className="muted small">Нет измеренных активных этапов. Это не подтверждение завершения задачи.</p>}
    {waits.map((wait, index) => <div className="processing-stage" key={`${wait.track_id}:${wait.provider}:${index}`}>
      <strong>{wait.filename} · {wait.provider}</strong><span>{reasons[wait.reason_code] || 'Ожидание сервиса'}</span>
      <small>{wait.retry_at ? `Повтор допустим после ${new Date(wait.retry_at * 1000).toLocaleString('ru-RU')}` : 'Время следующей попытки неизвестно'}{wait.overdue_wait ? ' · Срок ожидания прошел' : ''}</small>
    </div>)}
    {activity.provider_waits_truncated && <p className="muted small">Показаны первые 100 ожиданий.</p>}
    <details><summary>Измеренные этапы ({history.length})</summary>
      <p className="muted small">Ограниченная история событий. Для старых задач измерений может не быть; отсутствующие интервалы не восстанавливаются.</p>
      <ol className="processing-history">{history.map(stage => <li key={stage.stage_id} data-state={stage.state}>
        <strong>{stage.filename || 'Файл'} · {labels[stage.stage] || stage.stage}</strong>
        <span>{stage.provider ? `${stage.provider} · ` : ''}{states[stage.state] || stage.state} · {duration(stage.duration_ms === null ? null : stage.duration_ms / 1000)}{stage.http_status ? ` · HTTP ${stage.http_status}` : ''}</span>
        {stage.reason_code && <small>{reasons[stage.reason_code] || stage.reason_code}</small>}
        {stage.trace_url && <a href={stage.trace_url} target="_blank" rel="noopener noreferrer">Подробная трасса в Grafana</a>}
        {stage.trace_id && <small>Trace ID: {stage.trace_id}</small>}
      </li>)}</ol>
      {activity.stages_truncated && <p className="muted small">Показаны последние 100 этапов.</p>}
    </details>
  </section>;
}
