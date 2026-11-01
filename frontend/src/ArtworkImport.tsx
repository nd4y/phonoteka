import { useEffect, useState } from 'react';
import { ImagePlus } from 'lucide-react';
import { api, messageFor } from './api';
import type { SlotSelection } from './MediaSlotsPanel';

type ImageInfo = { width: number; height: number; bytes: number; mime: string; sha256: string };
export type ArtworkTransformDescriptor = { source: ImageInfo; result: ImageInfo; profile: { max_edge: number; max_bytes: number; format: string; effective_quality?: number | null } };
type Preview = { preview_id: string; cover: string; source: ImageInfo; result: ImageInfo; existing?: ImageInfo; smaller_than_existing: boolean; profile: { effective_quality?: number } };
export type ArtworkReceipt = { preview_id: string; confirm_smaller_cover: boolean };

export default function ArtworkImport({ trackId, sha256, selection, current, disabled, onApply }: { trackId: string; sha256: string; selection?: SlotSelection; current?: ArtworkTransformDescriptor; disabled: boolean; onApply: (cover: string, receipt: ArtworkReceipt) => void }) {
  const [source, setSource] = useState(''), [name, setName] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [maxEdge, setMaxEdge] = useState(1200), [maxKiB, setMaxKiB] = useState(1536), [format, setFormat] = useState('jpeg'), [quality, setQuality] = useState(88);
  const [preview, setPreview] = useState<Preview | null>(null), [confirm, setConfirm] = useState(false);
  useEffect(() => { setPreview(null); setConfirm(false); }, [selection?.inventory_sha256, selection?.cover_slot_id]);
  function changed() { setPreview(null); setConfirm(false); }
  async function open(file?: File) {
    if (!file) return;
    setError(''); changed();
    if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 20 * 1024 * 1024) { setError('Выберите JPEG или PNG до 20 МиБ и 25 мегапикселей.'); return; }
    const reader = new FileReader(); reader.onerror = () => setError('Не удалось прочитать изображение.');
    reader.onload = () => { setSource(String(reader.result)); setName(file.name.slice(0, 200)); }; reader.readAsDataURL(file);
  }
  async function prepare() {
    if (!source || !selection?.cover_slot_id) { setError('Сначала выберите целевой слот в разделе встроенных изображений ниже.'); return; }
    setBusy(true); setError(''); changed();
    try { setPreview(await api<Preview>(`/tracks/${encodeURIComponent(trackId)}/artwork-preview`, { method: 'POST', headers: { 'If-Match': sha256 }, body: JSON.stringify({ source, slot_selection: selection, max_edge: maxEdge, max_bytes: maxKiB * 1024, format, quality }) })); }
    catch (problem) { setError(messageFor(problem)); }
    finally { setBusy(false); }
  }
  function info(value: ImageInfo) { return `${value.width} × ${value.height} · ${(value.bytes / 1024).toFixed(1)} КиБ · ${value.mime}`; }
  return <div className="artwork-import">{current && <details className="muted small"><summary>Принятое преобразование текущей обложки</summary><p>Исходник: {info(current.source)} · SHA {current.source.sha256.slice(0, 16)}</p><p>Результат: {info(current.result)} · SHA {current.result.sha256.slice(0, 16)}</p><p>Профиль: {current.profile.format.toUpperCase()}, до {current.profile.max_edge} px и {Math.round(current.profile.max_bytes / 1024)} КиБ, без обрезки.</p></details>}<label className={`button secondary file-button ${disabled ? 'disabled' : ''}`}><ImagePlus size={18} />Загрузить исходную обложку<input type="file" accept="image/jpeg,image/png" disabled={disabled || busy} aria-label="Загрузить обложку" onChange={event => { void open(event.target.files?.[0]); event.target.value = ''; }} /></label>{error && <p role="alert" className="warning-text">{error}</p>}{source && <div className="artwork-transform"><h4>{name}</h4><p className="muted small">Настройте уменьшение без обрезки. Исходник не сохраняется на сервере; выбранный слот изменится только после сохранения предложения и записи файла.</p><div className="artwork-profile"><label>Большая сторона, px<input type="number" min={64} max={5000} value={maxEdge} disabled={disabled || busy} onChange={event => { setMaxEdge(Number(event.target.value)); changed(); }} /></label><label>Предел, КиБ<input type="number" min={32} max={10240} value={maxKiB} disabled={disabled || busy} onChange={event => { setMaxKiB(Number(event.target.value)); changed(); }} /></label><label>Формат<select value={format} disabled={disabled || busy} onChange={event => { setFormat(event.target.value); changed(); }}><option value="jpeg">JPEG</option><option value="png">PNG</option></select></label><label>Качество JPEG<input type="number" min={40} max={95} value={quality} disabled={disabled || busy || format === 'png'} onChange={event => { setQuality(Number(event.target.value)); changed(); }} /></label></div><button className="button secondary" disabled={disabled || busy || !selection?.cover_slot_id} onClick={() => void prepare()}>{busy ? 'Готовим предпросмотр...' : 'Сравнить исходник и результат'}</button>{!selection?.cover_slot_id && <p className="warning-text">Выберите целевой front cover или создание нового слота ниже.</p>}{preview && <><div className="artwork-comparison"><figure><img src={source} alt="Исходная обложка" /><figcaption>Исходник: {info(preview.source)}</figcaption></figure><figure><img src={preview.cover} alt="Обложка после преобразования" /><figcaption>Результат: {info(preview.result)}{preview.profile.effective_quality ? ` · качество ${preview.profile.effective_quality}` : ''}</figcaption></figure></div><p className="muted small">Пропорции сохранены, обрезки и увеличения нет. Метаданные изображения удаляются; прозрачность JPEG заменяется белым фоном. SHA результата: {preview.result.sha256.slice(0, 16)}.</p>{preview.existing && <p className="muted small">Существующий выбранный слот: {info(preview.existing)}</p>}{preview.smaller_than_existing && <label className="new-slot"><input type="checkbox" checked={confirm} onChange={event => setConfirm(event.target.checked)} disabled={disabled || busy} />Подтверждаю замену существующей обложки на изображение меньшего разрешения</label>}<div className="button-row"><button className="button primary" disabled={disabled || busy || preview.smaller_than_existing && !confirm} onClick={() => onApply(preview.cover, { preview_id: preview.preview_id, confirm_smaller_cover: confirm })}>Применить результат к черновику</button><button className="button subtle" disabled={busy} onClick={() => { setSource(''); changed(); }}>Закрыть предпросмотр</button></div></>}</div>}</div>;
}
