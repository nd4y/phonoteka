import { useEffect, useState } from 'react';
import { Disc3, RefreshCw, Save } from 'lucide-react';
import { api, messageFor } from './api';
import { AlbumBatchPanel } from './AlbumBatchPanel';
import './ReleaseReviewPanel.css';

type Album = { album: string; artist: string; ids: string[] };
type Member = { id: string; path: string; title: string; artist: string; discnumber: string; tracknumber: string; release_id: string };
type ReviewFile = { track_id: string; relative_path: string; reasons: string[]; duration_delta?: number | null; evidence?: string };
type ReleaseTrack = { slot: string; title: string; artist: string; duration?: number; expected: boolean; state: string; files: ReviewFile[] };
type Review = { id: string; release_id: string; version: number; state: string; stale: boolean; reference_state: string; reference?: { title: string; artist: string; date: string; country: string; source_url: string; disambiguation: string }; tracks: ReleaseTrack[]; subset_note: string; expected_slots: string[] | null; intentional_subset?: boolean; complete_release: boolean; matched_track_ids: string[]; counts: Record<string, number>; extra_files: ReviewFile[]; unresolved_files: ReviewFile[] };
type ReviewList = { id: string; release_id: string; files: number; created_at: string; summary?: { title?: string; state: string } };
const states: Record<string, string> = { unknown: 'Комплектность неизвестна', complete: 'Полный выбранный релиз', missing: 'Есть отсутствующие композиции', needs_review: 'Есть конфликты или лишние файлы', subset_complete: 'Выбранное подмножество собрано', subset_missing: 'Подмножество собрано не полностью', matched: 'Сопоставлено по метаданным', duplicate: 'Несколько файлов на позиции', conflict: 'Требует решения', not_expected: 'Не входит в ожидаемый состав' };
const reasons: Record<string, string> = { track_position_differs: 'Номер не совпадает с Release Track ID', track_position_missing_or_differs: 'Не совпадает позиция при совпавшем Recording ID', position_not_in_release: 'Позиции нет в релизе', position_unknown: 'Невозможно определить диск и номер', different_release_id: 'В файле ID другого издания', release_track_id_differs: 'Release Track ID отличается', recording_id_differs: 'Recording ID отличается', title_missing: 'Нет названия', title_differs: 'Название отличается', artist_missing: 'Нет исполнителя', artist_differs: 'Исполнитель отличается', duration_unknown: 'Неизвестна длительность', duration_differs: 'Длительность отличается более чем на 2 секунды' };

export function ReleaseReviewPanel({ albums, onOpen, onJob, onReplayGain }: { albums: Album[]; onOpen: (id: string) => void; onJob: (kind: string, ids: string[], params?: Record<string, unknown>) => Promise<unknown>; onReplayGain?: (ids: string[], params?: Record<string, unknown>) => Promise<unknown> }) {
  const [albumIndex, setAlbumIndex] = useState(''); const [members, setMembers] = useState<Member[]>([]); const [chosen, setChosen] = useState<string[]>([]);
  const [releaseId, setReleaseId] = useState(''); const [enabled, setEnabled] = useState(false); const [list, setList] = useState<ReviewList[]>([]);
  const [review, setReview] = useState<Review | null>(null); const [selectedId, setSelectedId] = useState(''); const [jobId, setJobId] = useState('');
  const [subset, setSubset] = useState(false); const [expected, setExpected] = useState<string[]>([]); const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false); const [memberLoading, setMemberLoading] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const selectedAlbum = albumIndex === '' ? null : albums[Number(albumIndex)];
  const subsetDirty = !!review && (subset !== (review.expected_slots !== null) || subset && (note !== review.subset_note || JSON.stringify([...expected].sort()) !== JSON.stringify([...(review.expected_slots || review.tracks.map(track => track.slot))].sort())));
  async function loadList() {
    const result = await api<{ items: ReviewList[] }>('/release-reviews'); setList(result.items);
  }
  function show(result: Review) {
    setReview(result); setSubset(result.expected_slots !== null); setExpected(result.expected_slots || result.tracks.map(track => track.slot)); setNote(result.subset_note || '');
  }
  function openFile(id: string) {
    if (subsetDirty) { setError('Сначала сохраните или отмените правки ожидаемого состава.'); return; }
    onOpen(id);
  }
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) { if (subsetDirty) { event.preventDefault(); event.returnValue = ''; } }
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [subsetDirty]);
  useEffect(() => {
    let active = true;
    void Promise.all([api<{ external_metadata: boolean }>('/providers'), api<{ items: ReviewList[] }>('/release-reviews')]).then(([providers, reviews]) => {
      if (active) { setEnabled(providers.external_metadata); setList(reviews.items); }
    }).catch(err => { if (active) setError(messageFor(err)); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setMembers([]); setChosen([]); setReleaseId(''); setMemberLoading(false);
    if (!selectedAlbum || selectedAlbum.ids.length > 200) return () => { active = false; };
    setMemberLoading(true);
    void api<{ items: Member[] }>('/release-reviews/members?ids=' + encodeURIComponent(selectedAlbum.ids.join(','))).then(result => {
      if (!active) return;
      setMembers(result.items); setChosen(result.items.map(member => member.id));
      const ids = [...new Set(result.items.map(member => member.release_id).filter(Boolean))];
      if (ids.length === 1) setReleaseId(ids[0]);
    }).catch(err => { if (active) setError(messageFor(err)); }).finally(() => { if (active) setMemberLoading(false); });
    return () => { active = false; };
  }, [selectedAlbum]);
  useEffect(() => {
    if (!selectedId) { setReview(null); return; }
    let active = true;
    void api<Review>('/release-reviews/' + encodeURIComponent(selectedId)).then(result => { if (active) show(result); }).catch(err => { if (active) setError(messageFor(err)); });
    return () => { active = false; };
  }, [selectedId]);
  useEffect(() => {
    if (!jobId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const job = await api<{ state: string; stage: string; retry_at?: number; error?: string }>('/jobs/' + encodeURIComponent(jobId));
        if (!active) return;
        if (job.state === 'succeeded') {
          await loadList(); if (!active) return; setSelectedId(jobId); setJobId(''); setNotice('Сверка завершена. Проверьте состав и конфликты ниже.'); return;
        }
        if (['failed', 'cancelled', 'cancelled_partial', 'interrupted', 'completed_with_errors'].includes(job.state)) {
          setJobId(''); setNotice('Сверка не завершена. Причина и результаты доступны в разделе «Обработка».'); return;
        }
        if (job.stage === 'waiting_provider') setNotice('MusicBrainz временно недоступен или достигнут лимит. Задание ждет автоматического повтора; файлы не отмечены сверенными.');
      } catch (err) { if (active) setError(messageFor(err)); }
      if (active) timer = setTimeout(() => void poll(), 4000);
    }
    void poll(); return () => { active = false; clearTimeout(timer); };
  }, [jobId]);
  async function queue() {
    setBusy(true); setError(''); setNotice('');
    try {
      const value = releaseId.trim().replace(/^https:\/\/(?:beta\.)?musicbrainz\.org\/release\//, '').replace(/\/$/, '');
      const result = await onJob('release_audit', chosen, { release_id: value });
      if (result && typeof result === 'object' && 'id' in result) { setJobId(String(result.id)); setNotice('Сверка релиза поставлена в очередь. Аудио и теги сохраняются.'); }
    } catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  async function saveSubset() {
    if (!review) return;
    setBusy(true); setError('');
    try {
      show(await api<Review>('/release-reviews/' + encodeURIComponent(review.id), { method: 'PUT', body: JSON.stringify({ base_version: review.version, expected_slots: subset ? expected : null, note: subset ? note : '' }) }));
      await loadList(); setNotice('Ожидаемый состав сохранен. Отсутствующие файлы не скачиваются и не считаются поврежденными.');
    } catch (err) { setError(messageFor(err)); } finally { setBusy(false); }
  }
  return <section className="panel settings-panel release-review-panel"><div className="panel-heading"><h2><Disc3 size={20} /> Сверка конкретного релиза</h2><button className="button subtle" disabled={busy || subsetDirty} onClick={() => { void loadList().catch(err => setError(messageFor(err))); if (selectedId) void api<Review>('/release-reviews/' + encodeURIComponent(selectedId)).then(show).catch(err => setError(messageFor(err))); }}><RefreshCw size={16} />Обновить</button></div>
    <p className="muted small">Название альбома в тегах лишь группирует файлы. Для проверки выберите конкретное издание MusicBrainz и его состав. Совпадение метаданных не подтверждает аудиоидентичность и не включает автозапись.</p>
    {error && <p className="warning-text" role="alert">{error}</p>}{notice && <p className="muted small" role="status">{notice}</p>}
    {!enabled && <p className="muted small">Внешние метаданные отключены. Сохраненные сверки доступны; новый эталонный список сейчас не запрашивается.</p>}
    <div className="release-review-form"><label>Группа файлов<select value={albumIndex} disabled={busy || !!jobId} onChange={event => setAlbumIndex(event.target.value)}><option value="">Выберите группу из отчета</option>{albums.map((album, index) => <option key={index} value={index}>{album.artist} · {album.album} · {album.ids.length} файлов</option>)}</select></label><label>Конкретный MusicBrainz release<input value={releaseId} disabled={busy || !!jobId} placeholder="UUID или https://musicbrainz.org/release/..." onChange={event => setReleaseId(event.target.value)} /></label></div>
    {selectedAlbum && selectedAlbum.ids.length > 200 && <p className="warning-text">В группе более 200 файлов. Разделите разные издания на группы до сверки.</p>}
    {!!members.length && <details><summary>Состав для сверки: выбрано {chosen.length} из {members.length}</summary><div className="release-member-list">{members.map(member => <label key={member.id}><input type="checkbox" checked={chosen.includes(member.id)} disabled={busy || !!jobId} onChange={event => setChosen(current => event.target.checked ? [...current, member.id] : current.filter(id => id !== member.id))} /><span><strong>{member.discnumber || '?'}:{member.tracknumber || '?'} · {member.artist} · {member.title || member.path}</strong><small className="muted break-word">{member.path}</small></span></label>)}</div></details>}
    <button className="button secondary" disabled={!enabled || busy || subsetDirty || memberLoading || !!jobId || !chosen.length || !releaseId.trim()} onClick={() => void queue()}>{jobId ? 'Сверка в обработке' : memberLoading ? 'Загрузка состава...' : 'Сверить с выбранным релизом'}</button>
    <label>Сохраненные сверки<select value={selectedId} disabled={busy || subsetDirty} onChange={event => setSelectedId(event.target.value)}><option value="">Выберите отчет</option>{list.map(item => <option key={item.id} value={item.id}>{item.summary?.title || item.release_id} · {states[item.summary?.state || 'unknown'] || item.summary?.state} · {item.files} файлов</option>)}</select></label>
    {review && <div className="release-review-result"><h3>{review.reference?.title || review.release_id}</h3><p className={`release-state ${review.complete_release ? 'complete' : ''}`} role="status">{states[review.state] || review.state}</p>
      {review.reference && <p className="muted small">{review.reference.artist} · {review.reference.date} · {review.reference.country}{review.reference.disambiguation ? ' · ' + review.reference.disambiguation : ''} · <a href={review.reference.source_url} target="_blank" rel="noreferrer">MusicBrainz</a></p>}
      {review.stale && <p className="warning-text">Версии файлов изменились. Этот отчет не подтверждает текущую комплектность; выполните новую сверку.</p>}
      {!review.reference && <p className="muted small">Эталон отсутствует, истек срок хранения или изменились разрешения источника. Комплектность остается неизвестной до новой сверки.</p>}
      <div className="badge-row">{Object.entries({ 'Ожидается': review.counts.expected, 'Найдено позиций': review.counts.found_slots, 'Не хватает': review.counts.missing, 'Конфликтов': review.counts.conflicts, 'Лишних файлов': review.counts.extra, 'Не определено': review.counts.unresolved }).map(([label, value]) => <span className="badge" key={label}>{label}: {value}</span>)}</div>
      <div className="button-row"><a className="button subtle" href={`/api/release-reviews/${encodeURIComponent(review.id)}/export/json`} download>JSON</a><a className="button subtle" href={`/api/release-reviews/${encodeURIComponent(review.id)}/export/csv`} download>CSV</a></div>
      {!!review.tracks.length && <><label className="release-subset-toggle"><input type="checkbox" checked={subset} disabled={busy || !!jobId || review.stale} onChange={event => setSubset(event.target.checked)} />Ожидаю только выбранное подмножество релиза</label>
        {subset && <label>Причина частичного состава<input value={note} disabled={busy || !!jobId || review.stale} onChange={event => setNote(event.target.value)} placeholder="Например, храню только первый диск" maxLength={1000} /></label>}
        <div className="release-track-list">{review.tracks.map(track => <article key={track.slot} className={`release-track ${track.state}`}><div className="release-track-heading">{subset && <input type="checkbox" aria-label={`Ожидать ${track.slot}: ${track.title}`} checked={expected.includes(track.slot)} disabled={busy || !!jobId || review.stale} onChange={event => setExpected(current => event.target.checked ? [...current, track.slot] : current.filter(slot => slot !== track.slot))} />}<strong>{track.slot} · {track.title}</strong><span className="badge">{states[track.state] || track.state}</span></div><p className="muted small">{track.artist}{track.duration ? ` · ${track.duration.toFixed(1)} с` : ''}</p>{track.files.map(file => <div key={file.track_id} className="release-matched-file"><button className="button subtle break-word" onClick={() => openFile(file.track_id)}>{file.relative_path}</button>{!!file.reasons.length && <p className="warning-text small">{file.reasons.map(reason => reasons[reason] || reason).join(' · ')}</p>}</div>)}</article>)}</div>
        <div className="button-row"><button className="button secondary" disabled={busy || !subsetDirty || review.stale || subset && (!expected.length || note.trim().length < 5)} onClick={() => void saveSubset()}><Save size={16} />Сохранить ожидаемый состав</button><button className="button subtle" disabled={busy || !subsetDirty} onClick={() => show(review)}>Отменить правки состава</button></div></>}
      {!![...review.extra_files, ...review.unresolved_files].length && <div><h4>Лишние и неопределенные файлы</h4>{[...review.extra_files, ...review.unresolved_files].map(file => <div key={file.track_id}><button className="button subtle break-word" onClick={() => openFile(file.track_id)}>{file.relative_path}</button><p className="muted small">{file.reasons.map(reason => reasons[reason] || reason).join(' · ')}</p></div>)}</div>}
      {onReplayGain && !!review.matched_track_ids.length && <button className="button secondary" disabled={busy || subsetDirty || review.stale || !['complete', 'subset_complete'].includes(review.state)} onClick={() => { setBusy(true); void onReplayGain(review.matched_track_ids, {release_review_id:review.id,release_review_version:review.version}).catch(err => setError(messageFor(err))).finally(() => setBusy(false)); }}>Измерить Album ReplayGain принятого состава</button>}
      {!review.stale && ['complete', 'subset_complete'].includes(review.state) && !!review.matched_track_ids.length && <div>{subsetDirty && <p className="muted small">Сначала сохраните или отмените правки состава. Групповой пакет использует сохраненную версию сверки.</p>}<div inert={subsetDirty || undefined}><AlbumBatchPanel ids={review.matched_track_ids} reviewId={review.id} reviewVersion={review.version} onOpen={openFile} /></div></div>}
    </div>}
  </section>;
}
