export type LyricLine = { id: string; time: number | null; text: string; wordsLocked?: boolean; timingLocked?: boolean };
export type ParsedLyrics = { lines: LyricLine[]; offset: number; warnings: string[]; metadata: Record<string, string[]> };
export function formatTime(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return '';
  const rounded = Math.round(ms);
  return `${String(Math.floor(rounded / 60000)).padStart(2, '0')}:${String(Math.floor(rounded / 1000) % 60).padStart(2, '0')}.${String(rounded % 1000).padStart(3, '0')}`;
}
export function parseTime(input: string): number | null {
  if (!input.trim()) return null;
  const match = /^(\d+):([0-5]\d)(?:[.:](\d{1,3}))?$/.exec(input.trim());
  if (!match) throw new Error('Время: мм:сс.ммм, например 01:24.500.');
  return Number(match[1]) * 60000 + Number(match[2]) * 1000 + Number((match[3] || '').padEnd(3, '0'));
}
export function parseLyrics(raw: string): ParsedLyrics {
  if (new TextEncoder().encode(raw).length > 2 * 1024 * 1024) throw new Error('Файл текста больше 2 МБ.');
  const rows = raw.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split('\n');
  if (rows.length > 20000) throw new Error('В тексте больше 20 000 строк.');
  const offsets = [...raw.matchAll(/^\s*\[offset:([+-]?\d+)\]\s*$/gmi)].map(m => Number(m[1]));
  if (offsets.length > 1 && new Set(offsets).size > 1) throw new Error('В LRC несколько разных offset. Уточните смещение в исходном файле.');
  const offset = offsets[0] || 0;
  const warnings: string[] = [];
  const lines: LyricLine[] = [];
  const metadata: Record<string, string[]> = {};
  rows.forEach((row, rowIndex) => {
    const meta = /^\s*\[([a-z][\w-]*):(.*)\]\s*$/i.exec(row);
    if (meta) { const key = meta[1].toLowerCase(); (metadata[key] ||= []).push(meta[2]); return; }
    if (/<\d+:\d{2}(?:\.\d+)?>/.test(row)) throw new Error('Enhanced LRC с таймкодами слов пока не поддерживается. Импортируйте построчный LRC.');
    const matches = [...row.matchAll(/\[(\d+:[0-5]\d(?:[.:]\d{1,3})?)\]/g)];
    if (matches.length) {
      const text = row.replace(/\[\d+:[0-5]\d(?:[.:]\d{1,3})?\]/g, '').trim();
      matches.forEach((match, index) => {
        const time = parseTime(match[1])! - offset;
        if (time < 0) throw new Error('Offset создает отрицательное время. Исправьте значение до импорта.');
        lines.push({ id: `${rowIndex}:${index}`, time, text });
      });
    } else if (row.trim()) {
      if (/\[\d+:/.test(row)) throw new Error(`Некорректный таймкод в строке ${rowIndex + 1}.`);
      lines.push({ id: `${rowIndex}:plain`, time: null, text: row });
    }
  });
  if (lines.every(line => line.time !== null)) lines.sort((a, b) => a.time! - b.time!);
  else if (lines.some(line => line.time !== null)) warnings.push('Есть строки без времени. Их порядок сохранен; расставьте таймкоды вручную.');
  if (offset) warnings.push(`LRC offset ${offset > 0 ? '+' : ''}${offset} мс применен: время строки минус offset. Повторно он не записывается.`);
  return { lines, offset, warnings, metadata };
}
export function serializeLyrics(lines: LyricLine[]): string {
  return lines.map(line => `${line.time === null ? '' : `[${formatTime(line.time)}]`}${line.text}`).join('\n');
}
export function validateLyrics(lines: LyricLine[], duration?: number): string | null {
  let previous = -1;
  for (const [index, line] of lines.entries()) {
    if (line.time === null) continue;
    if (!Number.isFinite(line.time) || line.time < 0) return `Строка ${index + 1}: время должно быть неотрицательным.`;
    if (duration && line.time > duration * 1000) return `Строка ${index + 1}: время выходит за длительность трека.`;
    if (line.time < previous) return `Строка ${index + 1}: время нарушает порядок строк. Переставьте строку или измените время.`;
    previous = line.time;
  }
  return null;
}
