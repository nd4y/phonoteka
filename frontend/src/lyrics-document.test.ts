import { describe, expect, it } from 'vitest';
import { editDocument, reconcileImportedLines } from './lyrics-document';

describe('durable lyric occurrences', () => {
  const lines = [{ id: 'chorus-first', text: 'Припев', time: 1000 }, { id: 'verse', text: 'Куплет', time: 2000 }, { id: 'chorus-second', text: 'Припев', time: 3000 }];
  it('keeps both chorus identities when times and words change', () => {
    const incoming = [{ ...lines[0], id: 'raw-0', time: 1500 }, { ...lines[1], id: 'raw-1', text: 'Правка куплета' }, { ...lines[2], id: 'raw-2', time: 3500 }];
    expect(reconcileImportedLines(lines, incoming).map(line => line.id)).toEqual(lines.map(line => line.id));
  });
  it('rejects changes to the locked axis while allowing the other axis', () => {
    const locked = [{ ...lines[0], wordsLocked: true }, { ...lines[1], timingLocked: true }];
    expect(reconcileImportedLines(locked, [{ ...locked[0], time: 1700 }, locked[1]])[0].time).toBe(1700);
    expect(() => reconcileImportedLines(locked, [{ ...locked[0], text: 'Other words' }, locked[1]])).toThrow('закрепленные');
    expect(() => reconcileImportedLines(locked, [locked[0]])).toThrow('закрепленную строку');
  });
  it('sends durable ID and explicit ordinal separately from nullable timing', () => {
    const edited = editDocument(7, [{ ...lines[2], time: null }, lines[0]], { 'x-source': ['owner'] });
    expect(edited.lines).toEqual([{ line_id: 'chorus-second', ordinal: 0, text: 'Припев', start_ms: null }, { line_id: 'chorus-first', ordinal: 1, text: 'Припев', start_ms: 1000 }]);
    expect(edited.base_version).toBe(7);
  });
});
