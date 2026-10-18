import { useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, RefObject } from 'react';
import { AudioLines, CornerDownLeft, LoaderCircle, Pause, Play, RefreshCw, Repeat1, RotateCcw, RotateCw, Square, Volume2, VolumeX } from 'lucide-react';
import { clampPosition, defaultPreferences, parsePlayerTime, playbackRates, playbackWindow, playerTime, preferences } from './player';
import './AudioPlayer.css';

type Props = {
  src: string; title?: string; subtitle?: string; duration?: number;
  start?: number; end?: number; autoPlay?: boolean;
  mediaRef?: RefObject<HTMLAudioElement | null>;
  onPosition?: (seconds: number) => void; onReady?: (ready: boolean) => void;
};
const preferenceKey = 'phonoteka-player';

export function AudioPlayer({ src, title = 'Прослушать аудио', subtitle = 'Текущая версия файла', duration = 0, start = 0, end, autoPlay = false, mediaRef, onPosition, onReady }: Props) {
  const internalRef = useRef<HTMLAudioElement>(null);
  const audio = mediaRef ?? internalRef;
  const id = useId();
  const [settings, setSettings] = useState(() => { try { return preferences(JSON.parse(localStorage.getItem(preferenceKey) || 'null')); } catch { return defaultPreferences; } });
  const [loaded, setLoaded] = useState(false);
  const [mediaDuration, setMediaDuration] = useState(duration);
  const [position, setPosition] = useState(start);
  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [buffered, setBuffered] = useState(0);
  const [repeat, setRepeat] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [jump, setJump] = useState('');
  const window = playbackWindow(mediaDuration, start, end);
  const ready = loaded && window.duration > 0 && !error;
  const elapsed = clampPosition(position, window.start, window.end) - window.start;
  const percent = window.duration ? elapsed / window.duration * 100 : 0;

  useEffect(() => {
    const player = audio.current;
    if (player) { player.volume = settings.volume; player.muted = settings.muted; player.playbackRate = settings.rate; }
    try { localStorage.setItem(preferenceKey, JSON.stringify(settings)); } catch { /* Playback also works without persistent storage. */ }
  }, [audio, settings]);

  useEffect(() => {
    const player = audio.current;
    return () => { player?.pause(); };
  }, [audio]);

  function seek(seconds: number) {
    const player = audio.current;
    if (!player || !ready) return;
    const next = clampPosition(seconds, window.start, window.end);
    player.currentTime = next; setPosition(next); onPosition?.(next); setMessage('');
  }

  async function play() {
    const player = audio.current;
    if (!player || !ready) return;
    if (player.currentTime >= window.end - 0.02) seek(window.start);
    setMessage('');
    try { await player.play(); } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return;
      setMessage('Не удалось начать воспроизведение. Нажмите кнопку еще раз или перезагрузите аудио.');
    }
  }

  function syncPosition() {
    const player = audio.current;
    if (!player || !loaded) return;
    const next = clampPosition(player.currentTime, window.start, window.end);
    setPosition(next); onPosition?.(next);
    if (player.currentTime >= window.end && !player.paused) {
      if (repeat) { player.currentTime = window.start; setPosition(window.start); onPosition?.(window.start); }
      else { player.pause(); player.currentTime = window.end; }
    }
  }

  function updateBuffer() {
    const player = audio.current;
    if (!player) return;
    let until = window.start;
    for (let i = 0; i < player.buffered.length; i++) {
      if (player.buffered.start(i) <= player.currentTime && player.buffered.end(i) >= player.currentTime) until = player.buffered.end(i);
    }
    setBuffered(window.duration ? (clampPosition(until, window.start, window.end) - window.start) / window.duration * 100 : 0);
  }

  function hotkey(event: KeyboardEvent<HTMLElement>) {
    const target = event.target as HTMLElement;
    if (event.altKey || event.ctrlKey || event.metaKey || target.closest('input, select, textarea, button, [contenteditable="true"]')) return;
    if (!ready) return;
    if (event.code === 'Space') { event.preventDefault(); if (playing) audio.current?.pause(); else void play(); }
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); seek(position + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 1 : 10)); }
    else if (event.key.toLowerCase() === 'm') { event.preventDefault(); setSettings({ ...settings, muted: !settings.muted }); }
    else if (event.key === 'Home') { event.preventDefault(); seek(window.start); }
    else if (event.key === 'End') { event.preventDefault(); seek(window.end); }
  }

  function jumpTo() {
    const seconds = parsePlayerTime(jump);
    if (seconds === null || seconds > window.duration) { setMessage(`Введите время от 0:00 до ${playerTime(window.duration, true)} в формате мм:сс или чч:мм:сс.`); return; }
    seek(window.start + seconds); setJump('');
  }

  return <section className="preview-player" aria-label="Плеер проверки аудио" tabIndex={0} onKeyDown={hotkey} aria-describedby={`${id}-keys`}>
    <audio ref={audio} src={src} preload="metadata"
      onLoadedMetadata={event => {
        const player = event.currentTarget;
        const limits = playbackWindow(player.duration, start, end);
        setMediaDuration(player.duration); setError(''); setLoaded(true); setWaiting(false); setPosition(limits.start);
        player.currentTime = limits.start; onPosition?.(limits.start); onReady?.(limits.duration > 0);
        if (!limits.duration) setError('Не удалось определить длительность аудио. Попробуйте загрузить его снова.');
        else if (autoPlay) void player.play().catch(() => setMessage('Нажмите воспроизведение, чтобы прослушать композицию.'));
      }}
      onDurationChange={event => { if (Number.isFinite(event.currentTarget.duration)) setMediaDuration(event.currentTarget.duration); }}
      onTimeUpdate={syncPosition} onProgress={updateBuffer}
      onPlay={() => setPlaying(true)} onPause={() => { setPlaying(false); setWaiting(false); }}
      onWaiting={() => setWaiting(true)} onPlaying={() => { setWaiting(false); setMessage(''); }} onCanPlay={() => { setWaiting(false); setMessage(''); }}
      onStalled={() => setMessage('Загрузка аудио задерживается. Можно подождать или загрузить его снова.')}
      onSeeking={event => { const player = event.currentTarget; const next = clampPosition(player.currentTime, window.start, window.end); if (next !== player.currentTime) player.currentTime = next; setPosition(next); onPosition?.(next); }}
      onEnded={() => { setPlaying(false); setWaiting(false); if (repeat) { seek(window.start); void play(); } }}
      onError={event => {
        const code = event.currentTarget.error?.code;
        setError(code === 2 ? 'Не удалось загрузить аудио. Проверьте соединение и повторите.' : code === 3 || code === 4 ? 'Браузер не поддерживает формат или не смог декодировать аудио. Откройте файл в обычном плеере или создайте редактируемую копию.' : 'Аудио недоступно. Обновите карточку и повторите загрузку.');
        setPlaying(false); setWaiting(false); setLoaded(false); onReady?.(false);
      }} />
    <div className="preview-heading"><span className="preview-icon"><AudioLines size={22} /></span><div><strong>{title}</strong><p>{subtitle}</p></div><span className={`preview-status ${playing ? 'is-playing' : ''}`} role="status">{error ? 'Недоступно' : !loaded ? 'Загрузка аудио' : waiting ? 'Буферизация' : playing ? 'Воспроизведение' : 'Пауза'}</span></div>
    <div className="preview-timeline"><div className="preview-range" style={{ '--played': `${percent}%`, '--buffered': `${buffered}%` } as CSSProperties}><input type="range" aria-label="Позиция воспроизведения" aria-valuetext={`${playerTime(elapsed, true)} из ${playerTime(window.duration, true)}`} min={0} max={window.duration || 1} step={0.1} value={elapsed} disabled={!ready} onChange={event => seek(window.start + Number(event.target.value))} /></div><div className="preview-times"><output className="mono" aria-label="Текущее время">{playerTime(elapsed, true)}</output><span className="mono" aria-label="Длительность">{playerTime(window.duration, true)}</span></div></div>
    <div className="preview-controls"><div className="preview-transport">
      <button type="button" className="icon-button" aria-label="Назад на 10 секунд" title="Назад на 10 секунд" disabled={!ready} onClick={() => seek(position - 10)}><RotateCcw size={21} /><span className="preview-skip">10</span></button>
      <button type="button" className="preview-play" aria-label={playing ? 'Приостановить' : 'Воспроизвести'} title={playing ? 'Приостановить' : 'Воспроизвести'} disabled={!ready} onClick={() => playing ? audio.current?.pause() : void play()}>{waiting || (!loaded && !error) ? <LoaderCircle size={24} className="spin" /> : playing ? <Pause size={24} fill="currentColor" /> : <Play size={24} fill="currentColor" />}</button>
      <button type="button" className="icon-button" aria-label="Вперед на 10 секунд" title="Вперед на 10 секунд" disabled={!ready} onClick={() => seek(position + 10)}><RotateCw size={21} /><span className="preview-skip">10</span></button>
      <button type="button" className="icon-button" aria-label="Остановить и вернуться к началу" title="Остановить и вернуться к началу" disabled={!ready} onClick={() => { audio.current?.pause(); seek(window.start); }}><Square size={17} /></button>
      <button type="button" className={`icon-button ${repeat ? 'is-selected' : ''}`} aria-label="Повтор композиции" title="Повтор композиции" aria-pressed={repeat} disabled={!ready} onClick={() => setRepeat(!repeat)}><Repeat1 size={20} /></button>
    </div><div className="preview-options"><div className="preview-volume"><button type="button" className="icon-button" aria-label={settings.muted ? 'Включить звук' : 'Выключить звук'} title={settings.muted ? 'Включить звук' : 'Выключить звук'} onClick={() => setSettings({ ...settings, muted: !settings.muted })}>{settings.muted || !settings.volume ? <VolumeX size={20} /> : <Volume2 size={20} />}</button><input type="range" aria-label="Громкость" min={0} max={1} step={0.05} value={settings.muted ? 0 : settings.volume} onChange={event => setSettings({ ...settings, volume: Number(event.target.value), muted: false })} /><span>{Math.round((settings.muted ? 0 : settings.volume) * 100)}%</span></div><label className="preview-speed"><span>Скорость</span><select aria-label="Скорость воспроизведения" value={settings.rate} onChange={event => setSettings({ ...settings, rate: Number(event.target.value) })}>{playbackRates.map(rate => <option key={rate} value={rate}>{rate}x</option>)}</select></label></div></div>
    <div className="preview-footer"><form className="preview-jump" onSubmit={event => { event.preventDefault(); jumpTo(); }}><label htmlFor={`${id}-jump`}>Перейти к</label><input id={`${id}-jump`} aria-label="Время перехода" placeholder="мм:сс.000" value={jump} disabled={!ready} onChange={event => setJump(event.target.value)} /><button type="submit" className="icon-button" aria-label="Перейти к указанному времени" title="Перейти к указанному времени" disabled={!ready || !jump.trim()}><CornerDownLeft size={17} /></button></form><details className="preview-help"><summary>Клавиши</summary><p id={`${id}-keys`}>В фокусе плеера: пробел - пауза; ← / → - 10 с; Shift + ← / → - 1 с; Home - начало; End - конец; M - звук. В полях ввода действуют обычные клавиши.</p></details></div>
    {(error || message) && <div className={`preview-message ${error ? 'has-error' : ''}`} role={error ? 'alert' : 'status'}><p>{error || message}</p><button type="button" className="button secondary" onClick={() => { setError(''); setMessage(''); setLoaded(false); setPlaying(false); setPosition(start); setBuffered(0); onReady?.(false); audio.current?.load(); }}><RefreshCw size={15} />Загрузить снова</button></div>}
  </section>;
}
