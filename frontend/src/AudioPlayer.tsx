import { useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, RefObject } from 'react';
import { Music2, MoreHorizontal, SkipBack, SkipForward, X, CornerDownLeft, LoaderCircle, Pause, Play, RefreshCw, Repeat1, RotateCcw, RotateCw, Square, Volume2, VolumeX } from 'lucide-react';
import { clampPosition, defaultPreferences, parsePlayerTime, playbackRates, playbackWindow, playerTime, preferences } from './player';
import './AudioPlayer.css';

type Props = {
  cover?: string; navigationBusy?: boolean; navigationLabel?: string;
  onPrevious?: (playing: boolean) => void; onNext?: (playing: boolean) => void;
  onOpen?: () => void; onClose?: () => void;
  src: string; title?: string; subtitle?: string; duration?: number;
  start?: number; end?: number; autoPlay?: boolean;
  initialPosition?: number;
  mediaRef?: RefObject<HTMLAudioElement | null>;
  onPosition?: (seconds: number) => void; onReady?: (ready: boolean) => void;
};
const preferenceKey = 'phonoteka-player';

export function AudioPlayer({ src, title = 'Прослушать аудио', subtitle = 'Текущая версия файла', duration = 0, cover, navigationBusy = false, navigationLabel, onPrevious, onNext, onOpen, onClose, start = 0, end, autoPlay = false, initialPosition, mediaRef, onPosition, onReady }: Props) {
  const internalRef = useRef<HTMLAudioElement>(null);
  const audio = mediaRef ?? internalRef;
  const id = useId();
  const autoplayPending = useRef(autoPlay);
  const [settings, setSettings] = useState(() => { try { return preferences(JSON.parse(localStorage.getItem(preferenceKey) || 'null')); } catch { return defaultPreferences; } });
  const [expanded, setExpanded] = useState(false);
  const [coverFailed, setCoverFailed] = useState(false);
  const container = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!expanded) return;
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node)) setExpanded(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [expanded]);
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

  return <section ref={container} className="dock-player" aria-label="Плеер проверки аудио" tabIndex={0} onKeyDown={event => { if (event.key === 'Escape') setExpanded(false); else hotkey(event); }}>
    <audio ref={audio} src={src} preload="metadata"
      onLoadedMetadata={event => {
        const player = event.currentTarget;
        const limits = playbackWindow(player.duration, start, end);
        setMediaDuration(player.duration); setError(''); setLoaded(true); setWaiting(false); setPosition(limits.start);
        const initial = clampPosition(initialPosition ?? limits.start, limits.start, limits.end);
        player.currentTime = initial; setPosition(initial); onPosition?.(initial); onReady?.(limits.duration > 0);
        if (!limits.duration) setError('Не удалось определить длительность аудио. Попробуйте загрузить его снова.');
        else if (autoplayPending.current) void player.play().catch(() => setMessage('Нажмите воспроизведение, чтобы прослушать композицию.'));
        autoplayPending.current = false;
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
    <div className="dock-progress" style={{ '--played': `${percent}%`, '--buffered': `${buffered}%` } as CSSProperties}><input type="range" aria-label="Позиция воспроизведения" aria-valuetext={`${playerTime(elapsed, true)} из ${playerTime(window.duration, true)}`} min={0} max={window.duration || 1} step={0.1} value={elapsed} disabled={!ready} onChange={event => seek(window.start + Number(event.target.value))}/></div>
    <div className="dock-row">
      <button type="button" className="dock-identity" onClick={onOpen} disabled={!onOpen || navigationBusy} aria-label="Открыть карточку текущего трека" title={`${title} · ${subtitle}`}><span className="dock-art"><Music2 size={22}/>{cover && !coverFailed && <img src={cover} alt="" onError={() => setCoverFailed(true)}/>}</span><span className="dock-track"><strong>{title}</strong><small>{subtitle}</small><span className="dock-context">{navigationLabel}</span></span></button>
      <div className="dock-transport">
        <button type="button" className="icon-button" aria-label="Предыдущий трек" title="Предыдущий трек" disabled={!onPrevious || navigationBusy} onClick={() => onPrevious?.(!!audio.current && !audio.current.paused)}><SkipBack size={22} fill="currentColor"/></button>
        <button type="button" className="dock-play" aria-label={playing ? 'Приостановить' : 'Воспроизвести'} title={playing ? 'Приостановить' : 'Воспроизвести'} disabled={!ready || navigationBusy} onClick={() => playing ? audio.current?.pause() : void play()}>{navigationBusy || waiting || (!loaded && !error) ? <LoaderCircle size={25} className="spin"/> : playing ? <Pause size={25} fill="currentColor"/> : <Play size={25} fill="currentColor"/>}</button>
        <button type="button" className="icon-button" aria-label="Следующий трек" title="Следующий трек" disabled={!onNext || navigationBusy} onClick={() => onNext?.(!!audio.current && !audio.current.paused)}><SkipForward size={22} fill="currentColor"/></button>
      </div>
      <div className="dock-actions"><span className="dock-time mono" aria-label="Время воспроизведения">{playerTime(elapsed)} / {playerTime(window.duration)}</span><span className="dock-volume"><button type="button" className="icon-button" aria-label={settings.muted ? 'Включить звук' : 'Выключить звук'} title={settings.muted ? 'Включить звук' : 'Выключить звук'} onClick={() => setSettings({ ...settings, muted: !settings.muted })}>{settings.muted || !settings.volume ? <VolumeX size={20}/> : <Volume2 size={20}/>}</button><input type="range" aria-label="Громкость" aria-valuetext={`${Math.round((settings.muted ? 0 : settings.volume) * 100)}%`} min={0} max={1} step={0.05} value={settings.muted ? 0 : settings.volume} onChange={event => setSettings({ ...settings, volume: Number(event.target.value), muted: false })}/></span><button type="button" className="icon-button" aria-label="Настройки плеера" aria-expanded={expanded} aria-controls={`${id}-tools`} title="Настройки плеера" onClick={() => setExpanded(!expanded)}><MoreHorizontal size={23}/></button></div>
    </div>
    <div className="dock-status" role="status">{error ? 'Аудио недоступно' : !loaded ? 'Загрузка аудио' : waiting ? 'Буферизация' : playing ? 'Воспроизведение' : 'Пауза'}<span> · {playerTime(elapsed, true)} / {playerTime(window.duration, true)}</span></div>
    {expanded && <div id={`${id}-tools`} className="dock-tools" aria-label="Настройки воспроизведения">
      <div className="dock-tools-heading"><strong>Проверка аудио</strong><button type="button" className="icon-button" aria-label="Закрыть настройки плеера" onClick={() => setExpanded(false)}><X size={18}/></button></div>
      <div className="dock-tools-row"><button type="button" className="button secondary" aria-label="Назад на 10 секунд" disabled={!ready} onClick={() => seek(position - 10)}><RotateCcw size={18}/>10 с</button><button type="button" className="button secondary" aria-label="Вперед на 10 секунд" disabled={!ready} onClick={() => seek(position + 10)}>10 с<RotateCw size={18}/></button><button type="button" className="icon-button" aria-label="Остановить и вернуться к началу" title="Остановить и вернуться к началу" disabled={!ready} onClick={() => { audio.current?.pause(); seek(window.start); }}><Square size={18}/></button><button type="button" className={`icon-button ${repeat ? 'is-selected' : ''}`} aria-label="Повтор композиции" title="Повтор композиции" aria-pressed={repeat} disabled={!ready} onClick={() => setRepeat(!repeat)}><Repeat1 size={20}/></button></div>
      <div className="dock-tools-row"><label className="dock-speed">Скорость<select aria-label="Скорость воспроизведения" value={settings.rate} onChange={event => setSettings({ ...settings, rate: Number(event.target.value) })}>{playbackRates.map(rate => <option key={rate} value={rate}>{rate}x</option>)}</select></label><span className="mono">{playerTime(elapsed, true)} / {playerTime(window.duration, true)}</span></div>
      <div className="dock-tools-volume"><button type="button" className="icon-button" aria-label={settings.muted ? 'Включить звук в настройках' : 'Выключить звук в настройках'} onClick={() => setSettings({ ...settings, muted: !settings.muted })}>{settings.muted ? <VolumeX size={20}/> : <Volume2 size={20}/>}</button><input type="range" aria-label="Громкость в настройках" min={0} max={1} step={0.05} value={settings.muted ? 0 : settings.volume} onChange={event => setSettings({ ...settings, volume: Number(event.target.value), muted: false })}/><span>{Math.round((settings.muted ? 0 : settings.volume) * 100)}%</span></div>
      <form className="dock-jump" onSubmit={event => { event.preventDefault(); jumpTo(); }}><label htmlFor={`${id}-jump`}>Перейти к</label><input id={`${id}-jump`} aria-label="Время перехода" placeholder="мм:сс.000" value={jump} disabled={!ready} onChange={event => setJump(event.target.value)}/><button type="submit" className="icon-button" aria-label="Перейти к указанному времени" disabled={!ready || !jump.trim()}><CornerDownLeft size={17}/></button></form>
      <p className="dock-help">В фокусе плеера: пробел - пауза; ← / → - 10 с; Shift + ← / → - 1 с; Home / End - границы; M - звук.</p>
      {onClose && <button type="button" className="button subtle" onClick={onClose}>Закрыть плеер</button>}
    </div>}
    {(error || message) && <div className={`dock-message ${error ? 'has-error' : ''}`} role={error ? 'alert' : 'status'}><p>{error || message}</p><button type="button" className="button secondary" onClick={() => { setError(''); setMessage(''); setLoaded(false); setPlaying(false); setPosition(start); setBuffered(0); onReady?.(false); audio.current?.load(); }}><RefreshCw size={15}/>Загрузить снова</button></div>}
  </section>;
}
