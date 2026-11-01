import type { LyricLine } from './lyrics';

export type LyricsDocumentLine = { line_id: string; ordinal: number; text: string; start_ms: number | null; words_state: string; timing_state: string };
export type LyricsDocument = { contract: string; document_id: string; version: number; file_revision: string; source: string; document_revision: string; words_revision: string; timings_revision: string; complete: boolean; lrc: string; parse_error?: string | null; metadata: Record<string, string[]>; lines: LyricsDocumentLine[]; locks: Record<string, { words_locked: boolean; timing_locked: boolean; words_sha256: string; start_ms: number | null }> };

export function documentLines(document?: LyricsDocument | null): LyricLine[] | undefined {
  return document?.lines.map(line => ({ id: line.line_id, text: line.text, time: line.start_ms,
    wordsLocked: !!document.locks[line.line_id]?.words_locked, timingLocked: !!document.locks[line.line_id]?.timing_locked }));
}

export function reconcileImportedLines(previous: LyricLine[], incoming: LyricLine[], newId: () => string = () => crypto.randomUUID()): LyricLine[] {
  const unused = new Set(previous.map(line => line.id));
  const groups = new Map<string, LyricLine[]>(), cursors = new Map<string, number>();
  previous.forEach(line => { const group = groups.get(line.text) || []; group.push(line); groups.set(line.text, group); });
  const matches = incoming.map(line => { const cursor = cursors.get(line.text) || 0; const old = groups.get(line.text)?.[cursor]; cursors.set(line.text, cursor + 1); if (old) unused.delete(old.id); return old; });
  const result = incoming.map((line, index) => {
    const old = matches[index] || (previous[index] && unused.has(previous[index].id) ? previous[index] : undefined);
    if (old) unused.delete(old.id);
    if (old?.wordsLocked && old.text !== line.text || old?.timingLocked && old.time !== line.time) throw new Error('Импорт изменяет закрепленные слова или время. Сначала снимите нужное закрепление.');
    return { ...line, id: old?.id || newId(), wordsLocked: old?.wordsLocked, timingLocked: old?.timingLocked };
  });
  const resultIds = new Set(result.map(line => line.id));
  if (previous.some(old => (old.wordsLocked || old.timingLocked) && !resultIds.has(old.id))) throw new Error('Импорт удаляет закрепленную строку. Сначала снимите закрепление.');
  return result;
}

export function editDocument(version: number, lines: LyricLine[], metadata: Record<string, string[]>) {
  return { base_version: version, metadata, lines: lines.map((line, ordinal) => ({ line_id: line.id, ordinal, text: line.text, start_ms: line.time })) };
}
