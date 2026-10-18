import { describe, expect, it } from 'vitest';
import { lyricEditorWarnings } from './LyricsDocumentEditor';
import { parseLyrics } from './lyrics';
import { reconcileImportedLines } from './lyrics-document';

describe('lyric import feedback follows the displayed document', () => {
  const baseline = [
    { id: 'first-occurrence', text: 'Первая строка', time: 0 },
    { id: 'second-occurrence', text: 'Вторая строка', time: 5000 },
  ];

  it('removes a stale partial warning on exact Undo and restores it on Redo', () => {
    const plain = parseLyrics('Первая строка\nВторая строка');
    const partial = parseLyrics('[00:00.000]Первая строка\nВторая строка');
    const plainRows = reconcileImportedLines(baseline, plain.lines);
    const partialRows = reconcileImportedLines(plainRows, partial.lines);
    expect(partial.warnings).toHaveLength(1);
    // These are the snapshots displayed by import, Undo, Undo, Redo, Redo.
    // The last import's warning array intentionally remains unchanged.
    const snapshots = [plainRows, partialRows, plainRows, baseline, plainRows, partialRows];
    expect(snapshots.map(lines => lyricEditorWarnings(lines, partial.warnings).current.length))
      .toEqual([0, 1, 0, 0, 0, 1]);
    expect(lyricEditorWarnings(baseline, partial.warnings).imported).toEqual([]);
    expect(partialRows.map(line => line.id)).toEqual(baseline.map(line => line.id));
    expect(baseline.map(line => line.time)).toEqual([0, 5000]);
  });

  it('retains the real offset import fact while recomputing timing completeness', () => {
    const parsed = parseLyrics('[offset:500]\n[00:01.000]Первая строка\nВторая строка');
    const current = lyricEditorWarnings(parsed.lines, parsed.warnings);
    expect(current.current).toHaveLength(1);
    expect(current.imported).toHaveLength(1);
    expect(current.imported[0]).toContain('offset +500 мс применен');
    expect(parsed.lines[0].time).toBe(500);
    const undone = lyricEditorWarnings(baseline, parsed.warnings);
    expect(undone.current).toEqual([]);
    expect(undone.imported).toEqual(current.imported);
    expect(lyricEditorWarnings(parsed.lines, parsed.warnings)).toEqual(current);
  });

  it('does not invent a partial warning for empty draft rows or swallow other diagnostics', () => {
    const blank = [...baseline, { id: 'new-empty', text: '', time: null }];
    const diagnostic = 'Формат исходного импорта требует проверки';
    expect(lyricEditorWarnings(blank, [diagnostic])).toEqual({ current: [], imported: [diagnostic] });
    expect(() => parseLyrics('[00:99.000]Некорректное время')).toThrow();
  });
});
