import { useEffect, useRef, useState } from 'react';
import { FolderCheck, Save, Upload } from 'lucide-react';
import { api, messageFor } from './api';
import { fieldLabels } from './TrackPolicyPanel';
import './AlbumBatchPanel.css';

type CoverChoice = { track_id: string; candidate_id: string; input_sha256: string; provider: string; available: boolean };
type Member = { id: string; relative_path: string; sha256: string; tags: Record<string, string | string[]>; base_proposal_id: string | null; locks: string[]; write_supported: boolean; existing_proposal_fields: string[]; cover_candidates: CoverChoice[] };
type Context = { items: Member[]; release_tags: Record<string, string> };
type Exception = { exclude?: boolean; skip_fields?: string[]; tags?: Record<string, string> };
type Plan = { track_id: string; relative_path: string; skipped_fields: string[]; changes: Array<{ field: string; before: unknown; after: unknown }>; error?: { code: string; message: string } | null };
type Preview = { preview_token: string; plans: Plan[]; excluded: Array<{ track_id: string; reason: string }>; saveable: boolean; proposal_bytes: number };
export type AlbumBatchManifest = { id: string; audio_changed: boolean; apply_job_created: boolean; entries: Array<{ track_id: string; proposal_id: string | null; state: string; changed_fields?: string[]; skipped_fields: string[] }>; excluded: Array<{ track_id: string; reason: string }> };
const fields = ['album', 'albumartist', 'date', 'originaldate', 'genre', 'composer', 'lyricist', 'musicbrainz_albumid'];
const text = (value: unknown) => Array.isArray(value) ? value.join('; ') : String(value ?? '');

export function AlbumBatchPanel({ ids, reviewId, reviewVersion, onOpen, onSaved }: { ids: string[]; reviewId?: string; reviewVersion?: number; onOpen: (id: string) => void; onSaved?: (value: AlbumBatchManifest) => void }) {
  const [context, setContext] = useState<Context | null>(null); const [values, setValues] = useState<Record<string, string>>({}); const [enabled, setEnabled] = useState<string[]>([]);
  const [releaseFields, setReleaseFields] = useState<string[]>([]); const [exceptions, setExceptions] = useState<Record<string, Exception>>({});
  const [cover, setCover] = useState<string | null>(null); const [source, setSource] = useState(''); const [lockMode, setLockMode] = useState('skip');
  const [preview, setPreview] = useState<Preview | null>(null); const [saved, setSaved] = useState<AlbumBatchManifest | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const request = useRef<Record<string, unknown> | null>(null); const saveKey = useRef(''); const serial = useRef(0);
  const identity = ids.join(',');
  const choices = context?.items.flatMap(item => item.cover_candidates).filter((item, index, list) => list.findIndex(other => other.candidate_id === item.candidate_id && other.track_id === item.track_id) === index) || [];
  const chosenSource = choices.find(item => `${item.track_id}|${item.candidate_id}` === source);
  const dirty = !!context && !saved && (!!enabled.length || !!cover || !!source || Object.keys(exceptions).length > 0);
  function invalidate() { setPreview(null); request.current = null; saveKey.current = ''; }
  useEffect(() => { serial.current += 1; setContext(null); setSaved(null); setPreview(null); setError(''); setBusy(false); return () => { serial.current += 1; }; }, [identity, reviewId, reviewVersion]);
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) { if (dirty) { event.preventDefault(); event.returnValue = ''; } }
    window.addEventListener('beforeunload', guard); return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);
  async function load() {
    const current = serial.current; setBusy(true); setError('');
    try {
      const params = new URLSearchParams({ ids: identity });
      if (reviewId && reviewVersion) { params.set('review_id', reviewId); params.set('review_version', String(reviewVersion)); }
      const result = await api<Context>('/album-batches/context?' + params);
      if (current !== serial.current) return;
      setContext(result); setSaved(null); setEnabled([]); setReleaseFields([]); setExceptions({}); setCover(null); setSource(''); invalidate();
      setValues(Object.fromEntries(fields.map(field => [field, result.items.every(item => text(item.tags[field]) === text(result.items[0].tags[field])) ? text(result.items[0].tags[field]) : ''])));
    } catch (err) { if (current === serial.current) setError(messageFor(err)); } finally { if (current === serial.current) setBusy(false); }
  }
  async function upload(file?: File) {
    if (!file) return;
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10 * 1024 ** 2) { setError('Выберите JPEG или PNG до 10 МиБ.'); return; }
    const current = serial.current;
    const reader = new FileReader(); reader.onload = () => { if (current === serial.current) { setCover(String(reader.result)); setSource(''); invalidate(); } }; reader.onerror = () => { if (current === serial.current) setError('Не удалось прочитать изображение.'); }; reader.readAsDataURL(file);
  }
  function exception(id: string, change: Partial<Exception>) { setExceptions(current => ({ ...current, [id]: { ...current[id], ...change } })); invalidate(); }
  function body() {
    return { track_ids: ids, revisions: Object.fromEntries(context!.items.map(item => [item.id, item.sha256])), base_proposals: Object.fromEntries(context!.items.map(item => [item.id, item.base_proposal_id])),
      tags: Object.fromEntries(enabled.filter(field => !releaseFields.includes(field)).map(field => [field, values[field] || ''])),
      release_fields: enabled.filter(field => releaseFields.includes(field)), release_review_id: reviewId || null, release_review_version: reviewVersion || null,
      cover, cover_source: chosenSource ? { track_id: chosenSource.track_id, candidate_id: chosenSource.candidate_id, input_sha256: chosenSource.input_sha256 } : null, exceptions, lock_mode: lockMode };
  }
  async function prepare() {
    setBusy(true); setError(''); const current = serial.current; const value = body();
    try { const result = await api<Preview>('/album-batches/preview', { method: 'POST', body: JSON.stringify(value) }); if (current === serial.current) { setPreview(result); request.current = value; saveKey.current = crypto.randomUUID(); } }
    catch (err) { if (current === serial.current) setError(messageFor(err)); } finally { if (current === serial.current) setBusy(false); }
  }
  async function save() {
    if (!preview || !request.current) return;
    setBusy(true); setError(''); const current = serial.current;
    try { const result = await api<AlbumBatchManifest>('/album-batches/save', { method: 'POST', headers: { 'Idempotency-Key': saveKey.current }, body: JSON.stringify({ ...request.current, preview_token: preview.preview_token }) }); if (current === serial.current) { setSaved(result); onSaved?.(result); } }
    catch (err) { if (current === serial.current) setError(messageFor(err)); } finally { if (current === serial.current) setBusy(false); }
  }
  return <div className="album-batch-panel">
    {!context && <button className="button secondary" disabled={busy || ids.length === 0 || ids.length > 200} onClick={() => void load()}><FolderCheck size={16} />{busy ? 'Загрузка состава...' : 'Групповые предложения тегов и обложки'}</button>}
    {ids.length > 200 && <p className="muted small">Для пакета выберите не более 200 файлов одного состава.</p>}
    {error && <p className="warning-text" role="alert">{error}</p>}
    {context && !saved && <><h3>Предложения для {context.items.length} файлов</h3><p className="muted small">Отметьте изменяемые поля. Существующие предложения текста и других полей сохраняются. Каждый файл проходит проверку версии, закреплений и прав источников. После сохранения откройте карточки для отдельной проверки и записи.</p>
      <div className="album-shared-fields">{fields.map(field => <div className="album-shared-field" key={field}><label className="album-field-toggle"><input type="checkbox" checked={enabled.includes(field)} disabled={busy} onChange={event => { setEnabled(current => event.target.checked ? [...current, field] : current.filter(value => value !== field)); invalidate(); }} />{fieldLabels[field] || field}</label><input aria-label={`Новое значение: ${fieldLabels[field] || field}`} value={releaseFields.includes(field) ? context.release_tags[field] || '' : values[field] || ''} disabled={busy || !enabled.includes(field) || releaseFields.includes(field)} onChange={event => { setValues(current => ({ ...current, [field]: event.target.value })); invalidate(); }} />{context.release_tags[field] && <label className="album-field-toggle small"><input type="checkbox" checked={releaseFields.includes(field)} disabled={busy || !enabled.includes(field)} onChange={event => { setReleaseFields(current => event.target.checked ? [...current, field] : current.filter(value => value !== field)); invalidate(); }} />Взять из выбранного релиза MusicBrainz</label>}</div>)}</div>
      <label>Закрепленные поля<select value={lockMode} disabled={busy} onChange={event => { setLockMode(event.target.value); invalidate(); }}><option value="skip">Пропустить защищенные поля и показать исключения</option><option value="reject">Остановить весь пакет при попытке изменения</option></select></label>
      <div className="album-cover-choice"><label className={`button secondary file-button ${busy ? 'disabled' : ''}`}><Upload size={16} />Загрузить общую обложку<input type="file" accept="image/png,image/jpeg" disabled={busy} onChange={event => { void upload(event.target.files?.[0]); event.target.value = ''; }} /></label><label>Обложка из найденного источника<select value={source} disabled={busy} onChange={event => { setSource(event.target.value); setCover(null); invalidate(); }}><option value="">Сохранить текущие обложки</option>{choices.map(choice => <option key={`${choice.track_id}|${choice.candidate_id}`} value={`${choice.track_id}|${choice.candidate_id}`} disabled={!choice.available}>{choice.provider} · {choice.candidate_id}{choice.available ? '' : ' · источник недоступен по политике'}</option>)}</select></label>{(cover || chosenSource) && <><img src={cover || '/api/album-batches/cover?' + new URLSearchParams({ track_id: chosenSource!.track_id, candidate_id: chosenSource!.candidate_id, input_sha256: chosenSource!.input_sha256 })} alt="Выбранная общая обложка" /><button className="button subtle" disabled={busy} onClick={() => { setCover(null); setSource(''); invalidate(); }}>Не менять обложки</button></>}</div>
      <details><summary>Индивидуальные исключения</summary><div className="album-batch-members">{context.items.map(item => <article key={item.id}><strong className="break-word">{item.relative_path}</strong><label className="album-field-toggle"><input type="checkbox" checked={!!exceptions[item.id]?.exclude} disabled={busy} onChange={event => exception(item.id, { exclude: event.target.checked })} />Исключить файл из пакета</label>{!item.write_supported && <p className="warning-text small">Файл доступен только для чтения; исключите его либо подготовьте поддерживаемую копию.</p>}<p className="muted small">{item.locks.length ? 'Закреплено: ' + item.locks.map(field => fieldLabels[field] || field).join(', ') : 'Закрепленных полей нет'}{item.existing_proposal_fields.length ? ' · Имеющееся предложение: ' + item.existing_proposal_fields.map(field => fieldLabels[field] || field).join(', ') : ''}</p>{!exceptions[item.id]?.exclude && [...enabled, ...(cover || source ? ['cover'] : [])].map(field => <div key={field} className="album-file-exception"><label className="album-field-toggle small"><input type="checkbox" checked={!!exceptions[item.id]?.skip_fields?.includes(field)} disabled={busy} onChange={event => { const skip = exceptions[item.id]?.skip_fields || []; const tags = { ...exceptions[item.id]?.tags }; delete tags[field]; exception(item.id, { skip_fields: event.target.checked ? [...skip, field] : skip.filter(value => value !== field), tags }); }} />Не менять {fieldLabels[field] || field}</label>{field !== 'cover' && <input aria-label={`Индивидуальное значение ${fieldLabels[field] || field} для ${item.relative_path}`} placeholder="Индивидуальное значение, если требуется" value={exceptions[item.id]?.tags?.[field] || ''} disabled={busy || exceptions[item.id]?.skip_fields?.includes(field)} onChange={event => { const tags = { ...exceptions[item.id]?.tags }; if (event.target.value) tags[field] = event.target.value; else delete tags[field]; exception(item.id, { tags }); }} />}</div>)}</article>)}</div></details>
      <div className="button-row"><button className="button secondary" disabled={busy || !dirty} onClick={() => void prepare()}>{busy ? 'Проверка...' : 'Показать изменения для каждого файла'}</button><button className="button subtle" disabled={busy} onClick={() => { serial.current += 1; setContext(null); invalidate(); }}>Отменить подготовку пакета</button></div>
      {preview && <div className="album-batch-preview"><h4>Предпросмотр</h4>{preview.plans.map(plan => <article key={plan.track_id}><strong className="break-word">{plan.relative_path}</strong>{plan.error && <p className="warning-text" role="alert">{plan.error.message}</p>}{plan.changes.map(change => <p key={change.field} className="small break-word"><strong>{fieldLabels[change.field] || change.field}:</strong> {change.field === 'cover' ? 'Текущая обложка будет заменена выбранной' : `${text(change.before) || 'Пусто'} → ${text(change.after) || 'Пусто'}`}</p>)}{!plan.error && !plan.changes.length && <p className="muted small">Изменений для этого файла нет.</p>}{!!plan.skipped_fields.length && <p className="muted small">Пропущено: {plan.skipped_fields.map(field => fieldLabels[field] || field).join(', ')}</p>}</article>)}{!!preview.excluded.length && <p className="muted small">Исключено файлов: {preview.excluded.length}</p>}<button className="button secondary" disabled={busy || !preview.saveable} onClick={() => void save()}><Save size={16} />Сохранить предложения пакета</button></div>}
    </>}
    {saved && <div role="status"><p>Пакет предложений сохранен. Аудиофайлы пока не изменены; задания записи не запускались.</p>{saved.entries.map(entry => <div key={entry.track_id}><button className="button subtle break-word" onClick={() => onOpen(entry.track_id)}>{context?.items.find(item => item.id === entry.track_id)?.relative_path || entry.track_id}</button><span className="muted small">{entry.state === 'proposed' ? 'Предложение сохранено' : 'Без изменений'}</span></div>)}<button className="button secondary" disabled={busy} onClick={() => void load()}>Подготовить новый пакет</button></div>}
  </div>;
}
