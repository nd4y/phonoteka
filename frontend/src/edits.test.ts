import { describe, expect, it } from 'vitest';
import { changedTags, editorBaseline, editorDraftChanged, lyricsDraftChanged, tagText } from './edits';
import type { EditorDraft } from './edits';
import type { Track } from './types';
import type { LyricsDocument } from './lyrics-document';
import { serializeLyrics } from './lyrics';
describe('proposals preserve untouched metadata', () => {
  it('a title edit does not flatten or send the multivalue artist', () => {
    const original = { title: 'Старое', artist: ['Первый', 'Второй'], album: 'Альбом' };
    expect(changedTags(original, { title: 'Новое', artist: tagText(original, 'artist'), album: 'Альбом' })).toEqual({ title: 'Новое' });
  });
  it('inherits unchanged proposal fields and explicitly sends a reverted field', () => {
    expect(changedTags({ title: 'Исходное', artist: 'Артист' }, { title: 'Исходное', artist: 'Артист' }, { title: 'Черновик' })).toEqual({ title: 'Исходное' });
    expect(changedTags({ title: 'Исходное' }, { title: 'Черновик' }, { title: 'Черновик' })).toEqual({});
  });
});

describe('editor dirty state describes the saved baseline, not interaction history', () => {
  const document: LyricsDocument = {
    contract: 'phonoteka.lyrics.v1', document_id: 'doc', version: 7, file_revision: 'current',
    source: 'proposal', document_revision: 'document-7', words_revision: 'words', timings_revision: 'times',
    complete: true, lrc: '[00:05.000]Первая\n[00:10.000]Припев',
    metadata: { 'x-private-note': ['сохранить', 'повтор'], ar: ['Артист'] },
    lines: [
      { line_id: 'occurrence-first', ordinal: 0, text: 'Первая', start_ms: 5000, words_state: 'proposed', timing_state: 'proposed' },
      { line_id: 'occurrence-chorus', ordinal: 1, text: 'Припев', start_ms: 10000, words_state: 'proposed', timing_state: 'proposed' },
    ],
    locks: { 'occurrence-first': { words_locked: true, timing_locked: false, words_sha256: 'words', start_ms: 5000 } },
  };
  function fixture(): Track {
    return {
      id: 'track', relative_path: 'album/track.flac', sha256: 'current', revision: 'current', format: 'flac',
      tags: { title: 'В файле', artist: ['Первый', 'Второй'] }, has_art: true, lrc: '[00:01.000]В файле',
      status: 'indexed', can_write: true, lyrics_document: structuredClone(document),
      proposal: { id: 'saved', input_sha256: 'current', tags: { title: 'Сохраненное предложение' }, lrc: document.lrc,
        slot_selection: { inventory_sha256: 'inventory', cover_slot_id: 'front', lyrics_slot_id: 'lyrics-ru' } },
    };
  }
  const baseline = () => editorBaseline(fixture(), ['title', 'artist']);
  const shift = (draft: EditorDraft, amount: number): EditorDraft => {
    const lines = draft.lines!.map(line => ({ ...line, time: line.time! + amount }));
    return { ...draft, lines, lrc: serializeLyrics(lines) };
  };

  it('clears dirty on exact Undo, permits Redo, and preserves IDs, locks and unknown metadata', () => {
    const base = baseline(), moved = shift(base, 100), undone = structuredClone(base);
    expect(editorDraftChanged(base, moved)).toBe(true);
    expect(editorDraftChanged(base, undone)).toBe(false);
    expect(lyricsDraftChanged(base, undone)).toBe(false);
    expect(editorDraftChanged(base, shift(undone, 100))).toBe(true);
    expect(editorDraftChanged(base, structuredClone(base))).toBe(false);
    expect(undone.lines![0]).toMatchObject({ id: 'occurrence-first', time: 5000, wordsLocked: true });
    expect(undone.metadata['x-private-note']).toEqual(['сохранить', 'повтор']);
  });

  it('uses a current saved proposal for manual field revert and ignores a stale proposal', () => {
    const base = baseline();
    expect(base.tags).toEqual({ title: 'Сохраненное предложение', artist: 'Первый; Второй' });
    const edit = { ...base, tags: { ...base.tags, title: 'Изменено' } };
    expect(editorDraftChanged(base, edit)).toBe(true);
    expect(editorDraftChanged(base, { ...edit, tags: { ...edit.tags, title: base.tags.title } })).toBe(false);
    expect(editorDraftChanged(base, { ...base, tags: { ...base.tags, title: 'В файле' } })).toBe(true);
    const track = fixture(); track.proposal!.input_sha256 = 'old';
    const stale = editorBaseline(track, ['title']);
    expect(stale.tags.title).toBe('В файле');
    expect(stale.lrc).toBe(track.lrc);
    expect(stale.slotSelection).toBeUndefined();
  });

  it('Undo of lyrics does not clear another field, cover receipt or slot change', () => {
    const base = baseline(), undone = structuredClone(base);
    const otherChanges: EditorDraft[] = [
      { ...undone, tags: { ...undone.tags, artist: 'Другой исполнитель' } },
      { ...undone, cover: 'data:image/jpeg;base64,new' },
      { ...undone, artworkReceipt: { preview_id: 'new-preview', confirm_smaller_cover: true } },
      { ...undone, slotSelection: { ...undone.slotSelection!, lyrics_slot_id: 'lyrics-en' } },
      { ...undone, slotSelection: { ...undone.slotSelection!, inventory_sha256: 'changed-inventory' } },
    ];
    for (const draft of otherChanges) {
      expect(lyricsDraftChanged(base, draft)).toBe(false);
      expect(editorDraftChanged(base, draft)).toBe(true);
    }
  });

  it('detects structured occurrence, order, lock and metadata changes even with the same LRC', () => {
    const base = baseline();
    const changes: EditorDraft[] = [
      { ...base, lines: base.lines!.map((line, index) => index ? line : { ...line, id: 'different-occurrence' }) },
      { ...base, lines: [...base.lines!].reverse() },
      { ...base, lines: base.lines!.map((line, index) => index ? line : { ...line, wordsLocked: false }) },
      { ...base, metadata: { ...base.metadata, 'x-private-note': ['заменено'] } },
    ];
    for (const draft of changes) expect(editorDraftChanged(base, draft)).toBe(true);
    expect(editorDraftChanged(base, { ...base, metadata: { ar: ['Артист'], 'x-private-note': ['сохранить', 'повтор'] } })).toBe(false);
  });

  it('does not dirty canonical timestamp formatting but retains a pending raw edit', () => {
    const base = baseline();
    expect(editorDraftChanged(base, { ...base, lrc: base.lrc.replace('.000', '.00') })).toBe(false);
    expect(editorDraftChanged(base, { ...base, lines: undefined, lrc: 'сырой незавершенный текст' })).toBe(true);
    expect(editorDraftChanged(base, { ...base, lines: undefined, lrc: base.lrc })).toBe(false);
    const legacy = { ...base, lines: undefined, lrc: '[00:05.00]Строка', metadata: {} };
    expect(editorDraftChanged(legacy, { ...legacy, lrc: '[00:05.000]Строка' })).toBe(false);
    expect(editorDraftChanged(legacy, { ...legacy, lrc: '[x-note:новое]\n[00:05.000]Строка' })).toBe(true);
  });

  it('creates a fresh baseline for a refreshed server document version and lock state', () => {
    const before = baseline(), refreshed = fixture();
    refreshed.lyrics_document!.version++;
    refreshed.lyrics_document!.locks['occurrence-first'].timing_locked = true;
    const after = editorBaseline(refreshed, ['title', 'artist']);
    expect(editorDraftChanged(after, structuredClone(after))).toBe(false);
    expect(after.lines![0].timingLocked).toBe(true);
    expect(editorDraftChanged(after, before)).toBe(true);
    expect(editorDraftChanged(before, structuredClone(before))).toBe(false);
  });
});
