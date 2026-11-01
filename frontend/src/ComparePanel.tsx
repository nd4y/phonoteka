import { useState } from 'react';
import { api, messageFor } from './api';

type Entry = { id: string; path: string };
type Compared = Entry & { sha256: string; format: string; duration?: number; size?: number; sample_rate?: number; channels?: number; bitrate?: number; bits_per_sample?: number; tags: Record<string, unknown>; has_art: boolean; has_lrc: boolean; audit?: { state?: string; warnings?: Array<{ message: string }> } };
type Comparison = { related_decisions: Array<{group_id:string;version:number;ids:string[]}>; tracks: Compared[]; version: number; decision_stale: boolean; classification: string; alignment: { reliable: boolean }; decision?: { mode: string; preferred_id: string } };

export function ComparePanel({ entries, onPreview }: { entries: Entry[]; onPreview?: (id: string, aligned: boolean, revisions: Record<string,string>) => void }) {
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const ids = entries.slice(0, 8).map(entry => entry.id);
  async function load() {
    setBusy(true); setError('');
    try { setData(await api<Comparison>('/reports/compare?ids=' + encodeURIComponent(ids.join(',')))); }
    catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  async function decide(mode: string, preferred?: string) {
    if (!data) return;
    setBusy(true); setError(''); setNotice('');
    try {
      setData(await api<Comparison>('/reports/compare/decision', { method: 'PUT', body: JSON.stringify({ ids, revisions: Object.fromEntries(data.tracks.map(track => [track.id, track.sha256])), version: data.version, supersedes: Object.fromEntries(data.related_decisions.map(group => [group.group_id, group.version])), mode, preferred_id: preferred || null }) }));
      setNotice('План сохранен. Аудиофайлы и личные поля остались на месте. Публикация и новая доставка выполняются отдельно.');
    } catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  return <div className="compare-panel">
    <button className="button secondary" disabled={busy} onClick={() => void load()}>{data ? 'Обновить сравнение' : 'Сравнить и выбрать копию'}</button>
    {entries.length > 8 && <p className="muted small">Показаны первые 8 файлов группы. Решение относится только к ним.</p>}
    {error && <p className="warning-text" role="alert">{error}</p>}
    {data && <>
      <p className="muted small">{data.classification === 'exact_bytes' ? 'Файлы совпадают побайтово.' : data.classification === 'same_packets' ? 'Проверенные аудиопакеты совпадают; метаданные могут отличаться.' : 'Кандидаты на одну запись. Исполнение и издание нужно проверить.'} {data.alignment.reliable ? 'A/B сохраняет позицию для проверенного одинакового аудио.' : 'Надежного сопоставления позиций нет. Каждый файл начинается с начала.'}</p>
      {!!data.related_decisions.length && <p className="warning-text">При сохранении полного состава предыдущие решения этих групп будут заменены новым. История сохранится.</p>}{data.decision_stale && <p className="warning-text">Версии изменились. Предыдущее решение требует повторной проверки.</p>}
      <div className="compare-grid">{data.tracks.map((track, index) => <article key={track.id} className={data.decision?.preferred_id === track.id ? 'preferred' : ''}>
        <strong>{String(track.tags.title || track.path)}</strong><p className="muted small break-word">{track.path}</p>
        <dl><dt>Формат</dt><dd>{track.format?.toUpperCase()} · {track.size == null ? '?' : (track.size / 1024 ** 2).toFixed(1)} МиБ</dd><dt>Длительность</dt><dd>{track.duration?.toFixed(3) || '?'} с</dd><dt>Параметры</dt><dd>{track.sample_rate || '?'} Гц · {track.channels || '?'} каналов · {track.bitrate ? Math.round(track.bitrate / 1000) : '?'} кбит/с · {track.bits_per_sample || '?'} бит</dd><dt>Обложка / LRC</dt><dd>{track.has_art ? 'Есть' : 'Нет'} / {track.has_lrc ? 'Есть' : 'Нет'}</dd><dt>Аудит</dt><dd>{track.audit?.state || 'Не выполнен'}</dd></dl>
        {track.audit?.warnings?.map((warning, i) => <p key={i} className="warning-text small">{warning.message}</p>)}
        <details><summary>Все теги и идентификаторы</summary><pre className="diagnostic">{JSON.stringify(track.tags, null, 2)}</pre></details>
        <div className="button-row"><button className="button subtle" disabled={!onPreview || busy} onClick={() => onPreview?.(track.id, data.alignment.reliable, Object.fromEntries(data.tracks.map(item => [item.id,item.sha256])))}>Слушать {String.fromCharCode(65 + index)}</button><button className="button secondary" disabled={busy} onClick={() => void decide('preferred', track.id)}>{data.decision?.preferred_id === track.id && !data.decision_stale ? 'Предпочтительная копия' : 'Выбрать для Ready'}</button></div>
      </article>)}</div>
      <div className="button-row"><button className="button subtle" disabled={busy} onClick={() => void decide('publish_both')}>Оставить все версии для публикации</button><button className="button subtle" disabled={busy} onClick={() => void decide('defer')}>Отложить решение</button></div>
      {data.decision && <p className="muted small">Решение v{data.version}: {({ preferred: 'предпочтительная копия', publish_both: 'все версии', defer: 'отложено' } as Record<string, string>)[data.decision.mode]}</p>}
    </>}
    {notice && <p className="small" role="status">{notice}</p>}
  </div>;
}
