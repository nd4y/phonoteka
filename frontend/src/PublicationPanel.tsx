import { useEffect, useRef, useState } from 'react';
import { ArchiveRestore, CircleAlert, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, messageFor } from './api';
import './PublicationPanel.css';
import { useBackLayer } from './useAppHistory';

type Version = { history_id: string; kind: string; created_at: string; ready_revision: string; restore_revision: string | null; profile: string; rollback_available: boolean };
type PlanItem = { track_id: string; relative_path: string; ready_path: string; work_revision: string; ready_revision: string | null; current_track: boolean; stale_ready: boolean; action: string; destination_exists: boolean; destination_integrity: string; actual_ready_revision: string | null; gate: { state: string; reasons: string[] }; versions: Version[] };
type Page = { items: PlanItem[]; page: number; page_size: number; total: number };
interface Props { trackId?: string; disabled?: boolean; onJobQueued?: () => void }
const actions: Record<string, string> = { add: 'Добавление', replace: 'Рабочая версия новее Ready', unchanged: 'Совпадает с рабочей ревизией', stale_ready: 'Устаревшая Ready', conflict: 'Конфликт назначения' };
const integrity: Record<string, string> = { not_checked: 'SHA-256 Ready ещё не проверен', matches: 'SHA-256 Ready совпадает с каталогом', conflict: 'Ready изменён вне приложения', missing: 'Файл Ready отсутствует', unsafe_or_unavailable: 'Назначение недоступно или путь небезопасен', untracked_destination: 'Файл назначения не зарегистрирован в истории' };

export default function PublicationPanel({ trackId, disabled = false, onJobQueued }: Props) {
  const [items, setItems] = useState<PlanItem[]>([]), [total, setTotal] = useState(0), [page, setPage] = useState(1);
  const [staleOnly, setStaleOnly] = useState(false), [generation, setGeneration] = useState(0);
  const [loading, setLoading] = useState(true), [verifying, setVerifying] = useState(''), [saving, setSaving] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [pending, setPending] = useState<{ item: PlanItem; version: Version } | null>(null);
  useBackLayer(!!pending, () => { if (!saving) setPending(null); }, 100);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setItems([]); setPending(null);
    const request = trackId ? api<PlanItem>(`/tracks/${encodeURIComponent(trackId)}/publication`).then(item => ({ items: [item], total: 1 }))
      : api<Page>(`/publication/plan?page=${page}&page_size=20&stale_only=${staleOnly}`);
    request.then(data => { if (active) { setItems(data.items); setTotal(data.total); } }).catch(err => { if (active) setError(messageFor(err)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [trackId, page, staleOnly, generation]);
  useEffect(() => { if (pending && dialog.current && !dialog.current.open) { dialog.current.showModal(); cancel.current?.focus(); } }, [pending]);
  async function verify(item: PlanItem) {
    setVerifying(item.track_id); setError('');
    try { const data = await api<PlanItem>(`/tracks/${encodeURIComponent(item.track_id)}/publication?verify=true`); setItems(old => old.map(value => value.track_id === data.track_id ? data : value)); }
    catch (err) { setError(messageFor(err)); } finally { setVerifying(''); }
  }
  async function rollback() {
    if (!pending || !pending.item.ready_revision || saving) return;
    setSaving(true); setError('');
    try {
      await api('/jobs', { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ kind: 'rollback_ready', track_ids: [pending.item.track_id], params: { history_id: pending.version.history_id, expected_ready_sha256: pending.item.ready_revision } }) });
      setPending(null); setNotice('Откат опубликованной версии принят. Результат появится в разделе «Обработка».'); onJobQueued?.();
    } catch (err) { setError(messageFor(err)); } finally { setSaving(false); }
  }
  return <section className="panel publication-panel" aria-label="План и история Music Ready">
    <div className="panel-heading"><div><h2>План и история Ready</h2><p className="muted small">Рабочая и опубликованная ревизии учитываются отдельно. Устаревшие файлы доступны для проверки.</p></div><button className="icon-button" aria-label="Обновить план Ready" disabled={loading || saving} onClick={() => setGeneration(value => value + 1)}><RefreshCw size={18} className={loading ? 'spin' : ''} /></button></div>
    {!trackId && <div className="publication-filters"><label><input type="checkbox" checked={staleOnly} onChange={event => { setStaleOnly(event.target.checked); setPage(1); }} />Только Ready другой или удалённой рабочей ревизии</label><span className="muted small">Записей: {total}</span></div>}
    {error && <div className="notice error" role="alert"><CircleAlert size={18} /><span>{error}</span></div>}{notice && <div className="notice success" role="status"><ShieldCheck size={18} /><span>{notice}</span></div>}
    {loading && <p className="publication-empty muted" role="status">Готовим план…</p>}
    {!loading && !items.length && <p className="publication-empty muted">{staleOnly ? 'Устаревших Ready по каталогу нет. Проверка хешей назначения выполняется отдельно.' : 'В плане пока нет файлов.'}</p>}
    {!loading && items.map(item => <article className="publication-item" key={item.track_id}><div className="publication-item-heading"><strong>{item.relative_path}</strong><span className={`badge ${item.action === 'conflict' || item.stale_ready ? 'warm' : 'purple'}`}>{actions[item.action] || item.action}</span></div><dl className="publication-revisions"><div><dt>Рабочая ревизия</dt><dd title={item.work_revision}>{item.work_revision.slice(0, 16)}</dd></div><div><dt>Опубликованная</dt><dd title={item.ready_revision || ''}>{item.ready_revision?.slice(0, 16) || 'Не опубликовано'}</dd></div><div><dt>Путь Ready</dt><dd>{item.ready_path}</dd></div></dl><div className="publication-check"><span className="muted small">{integrity[item.destination_integrity] || item.destination_integrity}</span><button className="button subtle" disabled={disabled || !!verifying || saving || !item.destination_exists} onClick={() => void verify(item)}>{verifying === item.track_id ? 'Проверяем SHA-256…' : 'Проверить SHA-256 Ready'}</button></div>{item.gate.state !== 'passed' && <p className="muted small">Новая публикация требует проверки профиля в карточке трека.</p>}{item.versions.length > 0 && <details><summary>История опубликованных версий ({item.versions.length})</summary><div className="publication-history">{item.versions.map(version => <div key={version.history_id}><span><strong>{version.kind === 'rollback_ready' ? 'Откат Ready' : 'Публикация'} · {new Date(version.created_at).toLocaleString('ru-RU')}</strong><small>Профиль: {version.profile}; Ready {version.ready_revision.slice(0, 12)}</small><small>{version.rollback_available ? `До операции: ${version.restore_revision?.slice(0, 12)}` : 'До первой публикации файла не было или резервная версия недоступна'}</small></span><button className="button secondary" disabled={disabled || saving || !version.rollback_available || !item.ready_revision || item.destination_integrity === 'conflict' || !item.current_track} onClick={() => setPending({ item, version })}><ArchiveRestore size={15} />Восстановить прежнюю Ready</button></div>)}</div></details>}</article>)}
    {!trackId && total > 20 && <div className="publication-pagination"><button className="button secondary" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}>Назад</button><span className="muted small">{page} из {Math.ceil(total / 20)}</span><button className="button secondary" disabled={page * 20 >= total || loading} onClick={() => setPage(value => value + 1)}>Далее</button></div>}
    <p className="publication-footer muted small">План не удаляет Ready и не меняет файлы на телефоне. Откат Ready восстанавливает сохранённую опубликованную версию; рабочее аудио и его теги сохраняются. Экспортированные поколения остаются отдельными снимками.</p>
    {pending && <dialog ref={dialog} className="publication-dialog" onCancel={event => { event.preventDefault(); if (!saving) setPending(null); }}><h2>Восстановить прежнюю Ready?</h2><p>{pending.item.ready_path}</p><p>Текущая Ready {pending.item.ready_revision?.slice(0, 12)} будет заменена сохранённой версией {pending.version.restore_revision?.slice(0, 12)}. Перед заменой сервер проверит ожидаемый хеш и сохранит текущую Ready для обратного отката.</p><p className="muted small">Рабочая версия {pending.item.work_revision.slice(0, 12)} остаётся прежней. После отката она может отличаться от Ready.</p>{error && <p className="warning-text" role="alert">{error}</p>}<div className="button-row"><button ref={cancel} className="button secondary" disabled={saving} onClick={() => setPending(null)}>Оставить текущую Ready</button><button className="button primary" disabled={saving} onClick={() => void rollback()}>{saving ? 'Создаём задачу…' : 'Восстановить Ready'}</button></div></dialog>}
  </section>;
}
