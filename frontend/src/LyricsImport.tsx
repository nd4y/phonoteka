import { useMemo, useState } from 'react';
import { Upload } from 'lucide-react';
import { parseLyrics } from './lyrics';
import { messageFor } from './api';

export type LyricsEncoding = 'utf-8' | 'windows-1251' | 'utf-16le' | 'utf-16be';
export function decodeLyrics(bytes: ArrayBuffer, encoding: LyricsEncoding): string {
  if (bytes.byteLength > 2 * 1024 * 1024) throw new Error('Файл текста больше 2 МиБ.');
  const text = new TextDecoder(encoding, { fatal: true }).decode(bytes);
  if (text.includes('\0')) throw new Error('В тексте есть нулевые символы. Проверьте выбранную кодировку.');
  parseLyrics(text);
  return text;
}

export default function LyricsImport({ disabled, onImport }: { disabled: boolean; onImport: (text: string) => void }) {
  const [file, setFile] = useState<{ name: string; bytes: ArrayBuffer } | null>(null);
  const [encoding, setEncoding] = useState<LyricsEncoding>('utf-8'), [error, setError] = useState('');
  const preview = useMemo(() => {
    if (!file) return null;
    try { const text = decodeLyrics(file.bytes, encoding); return { text, error: '', lines: text.split(/\r?\n/) }; }
    catch (problem) { return { text: '', lines: [], error: messageFor(problem) }; }
  }, [file, encoding]);
  async function open(candidate?: File) {
    if (!candidate) return;
    setError('');
    if (candidate.size > 2 * 1024 * 1024) { setError('Файл текста больше 2 МиБ.'); return; }
    try { setEncoding('utf-8'); setFile({ name: candidate.name.slice(0, 200), bytes: await candidate.arrayBuffer() }); }
    catch (problem) { setError(messageFor(problem)); }
  }
  return <><label className={`button secondary file-button ${disabled ? 'disabled' : ''}`}><Upload size={17} />Импорт LRC<input type="file" accept=".lrc,.txt,text/plain" disabled={disabled} aria-label="Импортировать LRC или TXT" onChange={event => { void open(event.target.files?.[0]); event.target.value = ''; }} /></label>{error && <p className="warning-text" role="alert">{error}</p>}{file && preview && <section className="notice info lyrics-import-preview" aria-label="Предпросмотр импорта текста"><div><h4>{file.name}</h4><label>Кодировка<select value={encoding} disabled={disabled} onChange={event => setEncoding(event.target.value as LyricsEncoding)}><option value="utf-8">UTF-8 (включая BOM)</option><option value="windows-1251">Windows-1251</option><option value="utf-16le">UTF-16 LE</option><option value="utf-16be">UTF-16 BE</option></select></label><p className="muted small">Проверьте слова и таймкоды. Текст попадет в черновик только после подтверждения; файл аудио не изменится.</p>{preview.error ? <p role="alert" className="warning-text">Не удалось разобрать текст: {preview.error} Выберите кодировку исходного файла.</p> : <><pre className="slot-text-preview">{preview.lines.slice(0, 200).join('\n')}</pre>{preview.lines.length > 200 && <p className="muted small">Показаны первые 200 из {preview.lines.length} строк.</p>}</>}<div className="button-row"><button className="button secondary" disabled={disabled || !!preview.error} onClick={() => { onImport(preview.text); setFile(null); }}>Применить к черновику</button><button className="button subtle" onClick={() => setFile(null)}>Отмена импорта</button></div></div></section>}</>;
}
