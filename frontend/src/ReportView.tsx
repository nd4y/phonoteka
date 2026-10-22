import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { ComparePanel } from './ComparePanel';
import { AlbumBatchPanel } from './AlbumBatchPanel';
import { SidecarPublicationPanel } from './SidecarPublicationPanel';
import { ReleaseReviewPanel } from './ReleaseReviewPanel';
import { YouTubeCollectionPanel } from './YouTubePanel';
import { CollectionOverview } from './CollectionOverview';
import type { CollectionFilter } from './CollectionOverview';
import { api, messageFor } from './api';

type Entry = { id: string; path: string; format: string; duration?: number; status: string };
type Report = { generated_at?: string; counts: Record<string, number>; exact_duplicates: Entry[][]; same_audio_packets: Entry[][];
  recording_candidates?: Array<{ evidence: string; warning: string; files: Entry[] }>;
  albums: Array<{ album: string; artist: string; files: number; missing_by_tags: Record<string, number[]>; duplicate_numbers: Record<string, number[]>; ids: string[] }> };
type Props = { onPreview?: (id: string, aligned: boolean, revisions: Record<string, string>) => void;
  onOpen: (id: string) => void; onJob: (kind: string, ids: string[], params?: Record<string, unknown>) => Promise<unknown>;
  onBrowse?: (filter: CollectionFilter) => void; onGuide?: () => void };

export function ReportView({ onOpen, onJob, onPreview, onBrowse, onGuide }: Props) {
  const [report, setReport] = useState<Report | null>(null), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(false), [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort(); const controller = new AbortController(); request.current = controller; setLoading(true);
    try { const value = await api<Report>('/reports/collection', { signal: controller.signal });
      if (!controller.signal.aborted) { setReport(value); setError(''); }
    } catch (problem) { if (!controller.signal.aborted) setError(messageFor(problem)); }
    finally { if (request.current === controller) setLoading(false); }
  }, []);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load]);
  async function gain(ids: string[]) {
    setBusy(true);
    try { const result = await onJob('replaygain_album', ids); if (result) setNotice('Измерение группы альбома поставлено в очередь. Проверьте состав группы и затем предложения в карточках треков.'); }
    catch (problem) { setError(messageFor(problem)); }
    finally { setBusy(false); }
  }
  return <div className="report-view">
    <section className="panel settings-panel" aria-label="Состояние коллекции" aria-busy={loading}>
      <div className="panel-heading"><div><span className="muted small">{loading ? 'Обновляем снимок коллекции...' : report?.generated_at ? `Снимок от ${new Date(report.generated_at).toLocaleString('ru-RU')}` : 'Данные текущего каталога'}</span></div><button className="button secondary" disabled={loading} onClick={() => void load()}><RefreshCw size={17}/>Обновить отчет</button></div>
      {error && <div className="notice error" role="alert">{error}{report && ' Показан предыдущий снимок; обновить данные не удалось.'}</div>}
      {notice && <p role="status">{notice}</p>}
      {report ? <CollectionOverview counts={report.counts} onBrowse={onBrowse} onGuide={onGuide}/> : <p role="status">{error ? 'Состояние коллекции пока недоступно.' : 'Загрузка отчета...'}</p>}
      <details className="overview-details"><summary>Скачать подробный отчет по файлам</summary><p className="muted small">Экспорт содержит пути, текущие состояния и причины незаполненных данных. JSON сохраняет структуру, CSV можно открыть в таблице.</p><div className="button-row"><a className="button subtle" href="/api/reports/export/json" download>Скачать JSON</a><a className="button subtle" href="/api/reports/export/csv" download>Скачать CSV</a></div></details>
    </section>
    <details className="panel settings-panel"><summary>Связи коллекции с YouTube</summary><YouTubeCollectionPanel/></details>
    {report && <>
      <ReleaseReviewPanel albums={report.albums} onOpen={onOpen} onJob={onJob} onReplayGain={(ids,params) => onJob('replaygain_album',ids,params)}/>
      <section className="panel settings-panel"><h2>Дубликаты</h2><p className="muted small">Побайтовое совпадение и совпадение проверенных аудиопакетов. Файлы сохраняются, сравнение не удаляет копии. Показаны первые 100 групп каждого вида.</p>{([['Полностью одинаковые файлы',report.exact_duplicates],['Одинаковые аудиопакеты',report.same_audio_packets]] as const).map(([label,groups]) => <div key={label}><h3>{label}</h3>{groups.length ? groups.map((group,index) => <details key={index}><summary>{group.length} файла · {group[0].path.split('/').pop()}</summary><ComparePanel entries={group} onPreview={onPreview}/>{group.map(entry => <button key={entry.id} className="button subtle break-word" onClick={() => onOpen(entry.id)}>{entry.path}</button>)}</details>) : <p className="muted small">Совпадений среди проверенных записей нет.</p>}</div>)}</section>
      {!!report.recording_candidates?.length && <section className="panel settings-panel"><h2>Кандидаты на одну запись</h2><p className="muted small">Совпадение MusicBrainz Recording ID или результата аудиоотпечатка. Это не подтвержденные дубликаты: live, cover, remix и ремастеры требуют отдельного решения.</p>{report.recording_candidates.map((group,index) => <details key={index}><summary>{group.files.length} файлов · {group.evidence === 'fingerprint_candidate' ? 'Аудиоотпечаток' : 'Recording ID'}</summary><ComparePanel entries={group.files} onPreview={onPreview}/></details>)}</section>}
      <section className="panel settings-panel"><h2>Альбомы</h2><p className="muted small">Группировка по тегам, до 500 альбомов. Пропуски определяются только при наличии общего числа треков в тегах. Комплектность конкретного релиза еще не подтверждена.</p>{report.albums.map((album,index) => <details key={index}><summary>{album.artist} · {album.album} · {album.files} файлов</summary><AlbumBatchPanel ids={album.ids} onOpen={onOpen}/><SidecarPublicationPanel ids={album.ids}/>{Object.entries(album.missing_by_tags).map(([disc,missing]) => <p key={disc}>Диск {disc}: {missing.length ? 'пропущены номера '+missing.join(', ') : 'пропусков по тегам нет'}</p>)}{Object.entries(album.duplicate_numbers).filter(([,numbers]) => numbers.length).map(([disc,numbers]) => <p key={disc}>Диск {disc}: повторяются номера {numbers.join(', ')}</p>)}<button className="button secondary" disabled={busy || album.ids.length > 200 || Object.values(album.duplicate_numbers).some(x => x.length > 0)} onClick={() => void gain(album.ids)}>Измерить ReplayGain группы альбома</button><p className="muted small">Группа определяется тегами. Проверьте состав; разные издания могут требовать отдельных измерений. При повторяющихся номерах измерение из этой группы отключено.</p><button className="button subtle" onClick={() => onOpen(album.ids[0])}>Открыть первый трек</button></details>)}</section>
    </>}
  </div>;
}
