import { useRef, useState } from 'react';
import { ArrowLeft, Disc3, RefreshCw, Play, CircleAlert } from 'lucide-react';
import type { Track, CueTrack } from './types';

const time = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

export function CueView({ track, onBack, onRefresh, onExtract, enabled, busy }: { track: Track; onBack: () => void; onRefresh: () => void; onExtract: () => void; enabled: boolean; busy: boolean }) {
  const [selected, setSelected] = useState<CueTrack | null>(null);
  const [error, setError] = useState('');
  const audio = useRef<HTMLAudioElement>(null);
  const document = track.cue!;
  const title = document.title || track.filename || 'Альбом CUE';
  function choose(item: CueTrack) {
    setError('');
    if (selected?.number === item.number && audio.current) {
      audio.current.currentTime = item.start_seconds;
      void audio.current.play().catch(() => setError('Нажмите воспроизведение в плеере.'));
    } else setSelected(item);
  }
  return <section className="panel editor-panel cue-panel">
    <div className="track-detail-heading"><button className="icon-button" onClick={onBack} aria-label="Назад к списку"><ArrowLeft /></button><Disc3 size={36} /><div className="track-detail-title"><span className="eyebrow">АЛЬБОМ CUE · ТОЛЬКО ЧТЕНИЕ</span><h2>{title}</h2><p>{document.performer || 'Исполнитель не указан'}</p><span className="badge">{document.tracks.length} композиций · {document.encoding || 'кодировка неизвестна'}</span></div><button className="icon-button" onClick={onRefresh} aria-label="Обновить карточку"><RefreshCw size={18} /></button></div>
    <p className="muted small">CUE задает названия и границы композиций. Аудиофайлы сохраняются целиком. Извлечение создает отдельные композиции в рабочей коллекции: FLAC для целочисленного PCM до 24 бит, WAV для остальных профилей. Затем доступны теги, тексты и публикация.</p>
    <div className="button-row"><button className="button primary" disabled={!enabled||busy||!!document.errors.length||track.status==='missing'} onClick={onExtract}>Извлечь композиции</button>{track.cue_export&&<span className="badge">Извлечено: {track.cue_export.count}</span>}</div><p className="muted small">Границы INDEX 01. INDEX 00 сохраняется в предыдущей композиции; звук до первой композиции сохраняется отдельно. Синтетическая тишина PREGAP не добавляется.</p>
    {!!document.errors.length && <div className="notice error cue-notice" role="alert"><CircleAlert size={20} /><div>{document.errors.map((item, index) => <p key={index}>{item.file && <strong className="break-word">{item.file}: </strong>}{item.message}</p>)}</div></div>}
    {!!document.warnings.length && <details className="cue-warnings"><summary>Уточнения связей с аудио ({document.warnings.length})</summary>{document.warnings.map((item, index) => <p className="small break-word" key={index}>{item.file}: {item.message}</p>)}</details>}
    <div className="cue-track-list" aria-label="Композиции CUE">{document.tracks.map(item => <button className={`cue-track ${selected?.number === item.number ? 'selected' : ''}`} key={item.number} disabled={!item.available || track.status === 'missing'} onClick={() => choose(item)} aria-label={`Слушать ${item.number}. ${item.title}`} aria-pressed={selected?.number === item.number}>
      <span className="mono">{String(item.number).padStart(2, '0')}</span><span className="cue-track-name"><strong>{item.title}</strong><small>{item.performer || document.performer}<br />{document.files[item.file_index]?.declared_name}</small></span><span className="cue-time">{item.available ? time(item.duration || 0) : 'Нет аудио'}<small>{time(item.start_seconds)}</small></span><Play size={17} />
    </button>)}</div>
    {selected && <div className="audio-preview cue-player"><strong>{selected.number}. {selected.title}</strong><p className="small muted">{time(selected.start_seconds)} - {time(selected.end_seconds!)} · шкала исходного файла. Плеер останавливается у следующей композиции.</p>
      <audio key={selected.number} ref={audio} controls preload="metadata" src={`/api/tracks/${encodeURIComponent(track.id)}/cue/${selected.number}/audio`} onLoadedMetadata={event => { event.currentTarget.currentTime = selected.start_seconds; void event.currentTarget.play().catch(() => setError('Нажмите воспроизведение в плеере.')); }} onTimeUpdate={event => { if (event.currentTarget.currentTime >= selected.end_seconds!) event.currentTarget.pause(); }} onSeeking={event => { const player = event.currentTarget; if (player.currentTime < selected.start_seconds) player.currentTime = selected.start_seconds; else if (player.currentTime > selected.end_seconds!) player.currentTime = selected.end_seconds!; }} onError={() => setError('Аудио недоступно или изменилось. Просканируйте коллекцию заново; проверьте поддержку формата браузером.')} />
      {error && <p className="warning-text small" role="alert">{error}</p>}
    </div>}
    <p className="quiet-note break-word">{track.relative_path}</p>
  </section>;
}
