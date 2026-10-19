import { CircleAlert, ShieldCheck } from 'lucide-react';
import { formatTime } from './lyrics';
import type { Track } from './types';
import './AudioAuditSummary.css';

const stages = { header: 'Чтение формата и аудиопотоков', packets: 'Проверка аудиопакетов', decode: 'Полное декодирование аудио' };
const reasons: Record<string, string> = {
  missing_file: 'Файл недоступен. Проверьте подключение источника и повторите сканирование.',
  input_error: 'Файл не удалось прочитать при сканировании. Актуальная целостность аудио не подтверждена.',
  invalid_audio: 'Декодирование или проверка аудиопакетов завершились ошибкой. Точная причина требует проверки записи.',
  unsupported_format: 'Проверка этого формата недоступна. Это отдельный результат, а не подтверждение повреждения.',
  verification_too_large: 'Достигнут безопасный лимит проверки. Это не доказательство повреждения аудио.',
  media_timeout: 'Истекло время проверки. Целостность аудио не подтверждена; это не доказательство повреждения.',
  tool_unavailable: 'Инструмент проверки недоступен. Целостность аудио не подтверждена.',
  file_changed: 'Файл изменился во время проверки. Проверьте актуальную версию повторно.',
};
const warningNames: Record<string, string> = {
  long_silence: 'Обнаружены продолжительные тихие участки; они могут быть частью записи.',
  high_sample_peak: 'Пик близок к 0 dBFS; это диагностический признак, а не подтвержденное повреждение.',
  possible_clipping: 'Возможен клиппинг. Подтвердите его прослушиванием.',
  signal_measurements_unavailable: 'Измерения сигнала недоступны; декодирование выполнено.',
  decoder_diagnostics: 'Декодер сообщил предупреждения без фатальной ошибки.',
  multiple_audio_streams: 'Проверены все аудиопотоки. Измерения сигнала и предпрослушивание относятся к первому.',
  decoder_channel_layout_inferred: 'Декодер определил расположение каналов по данным записи.',
};
const safeCode = (value: unknown): string => typeof value === 'string' && /^[a-z][a-z0-9_:-]{0,100}$/.test(value) ? value : '';
const hash = (value: unknown): string => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value) ? value : '';
const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
const finite = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
function codes(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.flatMap(item => { const code = safeCode(record(item)?.code); return code ? [code] : []; }))].slice(0, 100) : [];
}
type SilenceInterval = { start: number; end: number; duration: number };
function silenceIntervals(track: Track, audit: Record<string, unknown> | null, usable: boolean): { items: SilenceInterval[]; truncated: boolean } {
  const signal = record(audit?.signal);
  if (!usable || signal?.state !== 'measured' || signal.method !== 'ffmpeg_signal_v1' || !Array.isArray(signal.silence_intervals)) return { items: [], truncated: false };
  const bounds = [finite(track.duration), finite(audit?.duration)].filter((value): value is number => value !== undefined);
  if (!bounds.length) return { items: [], truncated: false };
  const endLimit = Math.min(...bounds);
  const items = signal.silence_intervals.slice(0, 100).flatMap(value => {
    const interval = record(value);
    const start = finite(interval?.start_seconds), end = finite(interval?.end_seconds), duration = finite(interval?.duration_seconds);
    return start !== undefined && end !== undefined && duration !== undefined && start < end && end <= endLimit && end <= Number.MAX_SAFE_INTEGER / 1000
      && duration > 0 && Math.abs(end - start - duration) <= 0.01 ? [{ start, end, duration }] : [];
  });
  return { items, truncated: signal.silence_intervals_truncated === true || signal.silence_intervals.length > 100 };
}

export default function AudioAuditSummary({ track, onSeek }: { track: Track; onSeek?: (seconds: number) => void }) {
  // The server owns classification, including input errors, stale revisions and
  // malformed legacy results. An older response without it cannot imply PASS.
  const status = ['error', 'warning', 'passed', 'unchecked', 'not_applicable'].includes(track.audit_status || '') ? track.audit_status! : 'unchecked';
  const audit = record(track.audit);
  const current = !!audit && hash(audit.sha256) === track.sha256;
  const stale = !!audit && !!hash(audit.sha256) && !current;
  const checked = status === 'passed' || status === 'warning';
  const error = status === 'error' ? safeCode(track.audit_error) : '';
  const stage = status === 'error' && track.audit_stage && track.audit_stage in stages ? track.audit_stage : '';
  const warnings = checked && current ? codes(audit?.warnings) : [];
  const notes = checked && current ? codes(audit?.notes) : [];
  const silence = silenceIntervals(track, audit, checked && current);
  const limited = ['verification_too_large', 'media_timeout', 'tool_unavailable'].includes(error);
  const title = status === 'not_applicable' ? 'CUE не является аудиофайлом'
    : status === 'error' ? error === 'unsupported_format' ? 'Формат не поддерживается' : limited ? 'Проверка не завершена' : 'Ошибка проверки аудио'
      : status === 'warning' ? 'Проверено с предупреждениями' : status === 'passed' ? 'Аудио проверено'
        : stale ? 'Проверка устарела' : 'Нет актуального результата проверки';
  const explanation = status === 'not_applicable' ? 'Проверять нужно связанные аудиофайлы. Сам CUE описывает композиции и их границы.'
    : status === 'error' ? reasons[error] || 'Проверка не завершилась успешно. Код причины сохранен; точный участок не определен.'
      : checked ? 'Сохранен успешный результат проверки текущей ревизии. Это не проверка музыкального содержания, тегов или точности текста.'
        : stale ? 'Результат относится к другой версии файла и не подтверждает текущую. Повторите проверку аудио.'
          : 'Для текущей версии нет подтвержденного результата. Выполните проверку аудио.';
  const diagnostic = {
    audit_status: status, audit_error: error || undefined, audit_stage: stage || undefined,
    current_sha256: hash(track.sha256), audit_sha256: hash(audit?.sha256) || undefined,
    ...(current ? { analyzer: safeCode(audit?.analyzer) || undefined, profile: safeCode(audit?.profile) || undefined,
      audio_stream_count: finite(audit?.audio_stream_count), duration: finite(audit?.duration), warnings, notes,
      silence_intervals: silence.items.length ? silence.items : undefined, silence_intervals_truncated: silence.truncated || undefined } : {}),
  };
  return <section className="audio-audit-summary" aria-label="Результат проверки аудио">
    <div className={`notice ${error === 'unsupported_format' ? 'info' : status === 'error' && !limited ? 'error' : status === 'warning' || limited ? 'warning' : checked ? 'success' : 'info'}`}>
      {checked ? <ShieldCheck size={19} /> : <CircleAlert size={19} />}<div><h3>{title}</h3><p>{explanation}</p></div>
    </div>
    <dl className="file-facts"><div><dt>Текущая ревизия</dt><dd>{checked ? 'Проверена' : status === 'not_applicable' ? 'Проверяется в связанных аудиофайлах' : 'Не подтверждена'}</dd></div>
      {error && <div><dt>Код причины</dt><dd className="mono break-word">{error}</dd></div>}
      {status === 'error' && <div><dt>Этап</dt><dd>{stage ? stages[stage] : 'Не зафиксирован'}</dd></div>}
      {(status === 'error' || status === 'warning') && <div><dt>Привязка ко времени</dt><dd>{silence.items.length ? 'Границы тихих участков показаны ниже. У других предупреждений временные границы могут отсутствовать.' : 'Неизвестен: временные границы не зафиксированы.'}</dd></div>}
    </dl>
    {!!silence.items.length && <><h3>Тихие участки</h3><p className="muted small">Тише -60 dBFS по измерению сигнала. Это может быть частью записи; переход к участку не запускает воспроизведение.</p><ol className="audit-interval-list">{silence.items.map((interval, index) => <li key={`${index}:${interval.start}`}><div><strong>{formatTime(Math.round(interval.start * 1000))} - {formatTime(Math.round(interval.end * 1000))}</strong><small>Длительность {interval.duration.toLocaleString('ru-RU', { maximumFractionDigits: 3 })} с</small></div>{onSeek && <button type="button" className="button secondary" onClick={() => onSeek(interval.start)} aria-label={`Перейти к тихому участку ${index + 1}, ${formatTime(Math.round(interval.start * 1000))}`}>К началу участка</button>}</li>)}</ol>{silence.truncated && <p className="muted small">Показано интервалов: {silence.items.length}. Список ограничен 100 интервалами; в записи могут быть и другие тихие участки.</p>}</>}
    {!!warnings.length && <><h3>Предупреждения</h3><ul className="audit-issue-list">{warnings.map(code => <li key={code}>{warningNames[code] || 'Дополнительное предупреждение проверки.'}<small className="mono break-word">{code}</small></li>)}</ul></>}
    {!!notes.length && <><h3>Примечания декодера</h3><ul className="audit-issue-list">{notes.map(code => <li key={code}>{warningNames[code] || 'Дополнительное примечание проверки.'}<small className="mono break-word">{code}</small></li>)}</ul></>}
    {checked && <p className="muted small">Фильтр «Аудио: проверено» включает записи с предупреждениями. Прослушивание остается отдельной проверкой.</p>}
    <details><summary>Диагностические данные проверки</summary><pre className="diagnostic">{JSON.stringify(diagnostic, null, 2)}</pre></details>
  </section>;
}
