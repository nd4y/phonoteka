import { useEffect, useState } from 'react';
import { FileDown, RefreshCw, Save, ShieldCheck } from 'lucide-react';
import { api, messageFor } from './api';
import type { Candidate } from './types';

type Rule = { provider: string; data_type: string; policy_version: string; allow_fetch: boolean; allow_cache: boolean; allow_embed: boolean; allow_export: boolean; allow_derived_analysis: boolean; cache_ttl: number; basis: string; terms_url: string; terms_note: string; rights_status: string; checked_at: string; review_after: string; attribution: string };
type Policy = { providers: Array<{ id: string; rules: Rule[] }>; notice: string };
export type PolicyCandidate = Candidate & { transient_only?: boolean; policy_review_required?: boolean; usage?: Partial<Rule> & { origin?: string; license_verified?: boolean } };
const operationNames = { allow_fetch: 'Получать данные', allow_cache: 'Хранить в кеше и предложениях', allow_embed: 'Вшивать в аудиофайл', allow_export: 'Копировать в Ready и экспортировать', allow_derived_analysis: 'Использовать в ASR и анализе' } as const;
const typeNames: Record<string, string> = { metadata: 'Метаданные', lyrics: 'Текст песни', cover: 'Обложка', provenance: 'Ссылка на источник' };

export function ProviderPolicyPanel() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [provider, setProvider] = useState('lrclib');
  const [kind, setKind] = useState('lyrics');
  const [draft, setDraft] = useState<Rule | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  async function load() {
    setError('');
    try { setPolicy(await api<Policy>('/provider-policy')); } catch (err) { setError(messageFor(err)); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => { setDraft(policy?.providers.find(item => item.id === provider)?.rules.find(rule => rule.data_type === kind) || null); }, [policy, provider, kind]);
  async function save() {
    if (!draft) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const { allow_fetch, allow_cache, allow_embed, allow_export, allow_derived_analysis, cache_ttl, basis, terms_url, attribution, data_type, policy_version } = draft;
      await api(`/provider-policy/${encodeURIComponent(provider)}`, { method: 'PUT', body: JSON.stringify({ allow_fetch, allow_cache, allow_embed, allow_export, allow_derived_analysis, cache_ttl, basis, terms_url, attribution, data_type, base_version: policy_version }) });
      await load(); setNotice('Политика сохранена для новых запросов. Повторите поиск, чтобы получить данные с новой версией разрешений. Музыка и резервные версии сохранены.');
    } catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  async function report() {
    setBusy(true); setError('');
    try {
      const result = await api<unknown>('/reports/provenance');
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'phonoteka-provenance.json'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  return <section className="panel settings-panel settings-wide provider-policy-panel">
    <div className="section-intro"><h2><ShieldCheck size={20} aria-hidden="true" /> Права использования источников</h2><button className="icon-button" disabled={busy} aria-label="Обновить политики источников" onClick={() => void load()}><RefreshCw size={17} /></button></div>
    <p className="muted small">{policy?.notice || 'Для получения, хранения, вшивания, экспорта и анализа действуют отдельные разрешения.'}</p>
    <p className="muted small">Существующие теги, текст и обложка в личных файлах сохраняются с отметкой о неизвестном происхождении. Ручная правка не снимает известные ограничения полученного источника.</p>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice success" role="status">{notice}</p>}
    <div className="button-row" style={{ margin: '18px 0' }}><label>Источник<select value={provider} disabled={busy} onChange={event => { setProvider(event.target.value); setNotice(''); }}>
      {(policy?.providers || [{ id: 'lrclib' }]).map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label>
      <label>Тип данных<select value={kind} disabled={busy} onChange={event => { setKind(event.target.value); setNotice(''); }}>{Object.entries(typeNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
    {draft && <><p className="muted small">{draft.terms_note}{draft.terms_url && <> <a href={draft.terms_url} target="_blank" rel="noreferrer">Условия источника</a></>}</p>
      <p className="muted small">Проверка условий: {draft.checked_at.slice(0, 10)} · повторная проверка до {draft.review_after}. Статус: {draft.rights_status === 'unknown' ? 'Права на контент не установлены' : draft.rights_status === 'owner_asserted' ? 'Основание указал владелец' : draft.rights_status}.</p>
      <div className="capability-list" style={{ margin: '16px 0' }}>{Object.entries(operationNames).map(([key, label]) => <label key={key} style={{ flexDirection: 'row', alignItems: 'center', margin: '9px 0' }}>
        <input type="checkbox" checked={draft[key as keyof typeof operationNames]} disabled={busy} onChange={event => setDraft({ ...draft, [key]: event.target.checked })} />{label}</label>)}</div>
      <div className="settings-grid"><label>Срок кеша, часов<input type="number" min={0} max={720} step={1} value={draft.cache_ttl / 3600} disabled={busy || !draft.allow_cache} onChange={event => setDraft({ ...draft, cache_ttl: Math.round(Number(event.target.value) * 3600) })} /></label>
        <label>Атрибуция<input value={draft.attribution} maxLength={500} disabled={busy} onChange={event => setDraft({ ...draft, attribution: event.target.value })} /></label></div>
      <label style={{ marginTop: 16 }}>Основание выбранных прав<textarea rows={3} maxLength={2000} placeholder="Укажите лицензию, разрешение правообладателя или другое проверенное основание для выбранных операций" value={draft.basis} disabled={busy} onChange={event => setDraft({ ...draft, basis: event.target.value })} /></label>
      <label style={{ marginTop: 12 }}>Ссылка на условия<input type="url" maxLength={500} value={draft.terms_url} disabled={busy} onChange={event => setDraft({ ...draft, terms_url: event.target.value })} /></label>
      <p className="muted small" style={{ marginTop: 14 }}>LRCLIB: для сохранения предложения нужны кеш и основание; для записи нужен embed; для Ready нужен export; для выравнивания текста нужен derived analysis. Для CAA те же операции задаются отдельно для обложки. По истечении срока кеша содержимое полученных предложений очищается, ожидающие задачи с ним отменяются. Эти настройки фиксируют решение владельца и не выдают лицензию.</p>
      <div className="button-row" style={{ marginTop: 18 }}><button className="button secondary" disabled={busy || draft.basis.trim().length < 5 || draft.allow_cache && draft.cache_ttl <= 0} onClick={() => void save()}><Save size={17} />Сохранить права использования</button>
        <button className="button subtle" disabled={busy} onClick={() => void report()}><FileDown size={17} />Отчет происхождения</button></div></>}
  </section>;
}

export function ProviderCandidateUsage({ trackId, candidate }: { trackId: string; candidate: PolicyCandidate }) {
  const [preview, setPreview] = useState<{ cover?: string; lrc?: string; plain_lyrics?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const usage = candidate.usage;
  if (!usage && !candidate.transient_only) return null;
  async function open() {
    setBusy(true); setError('');
    try { setPreview(await api(`/tracks/${encodeURIComponent(trackId)}/provider-preview`, { method: 'POST', body: JSON.stringify({ candidate_id: candidate.id }) })); }
    catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  return <div className="small" style={{ marginTop: 12 }}>
    {candidate.transient_only ? <p className="warning-text">Содержимое не хранится: права на кеш не установлены или срок истек. В настройках источников можно указать проверенное основание и нужные операции, затем повторить поиск.</p>
      : usage?.origin === 'provider_acquired' && <p className="muted">Разрешенные операции: {Object.entries(operationNames).filter(([key]) => usage[key as keyof typeof operationNames]).map(([, label]) => label.toLowerCase()).join(', ') || 'только просмотр'}. Лицензия на конкретный контент не подтверждена приложением.</p>}
    {candidate.transient_only && ['lrclib', 'coverartarchive'].includes(candidate.provider) && <button className="button subtle" disabled={busy} onClick={() => void open()}>{busy ? 'Получаем предпросмотр...' : 'Предпросмотр без сохранения'}</button>}
    {error && <p className="warning-text" role="alert">{error}</p>}
    {preview?.cover && <img className="candidate-cover" src={preview.cover} alt="Временный предпросмотр обложки" />}
    {(preview?.lrc || preview?.plain_lyrics) && <pre>{preview.lrc || preview.plain_lyrics}</pre>}
    {preview && <p className="muted">Предпросмотр хранится только в памяти этой карточки.</p>}
  </div>;
}
