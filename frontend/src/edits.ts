import type { Tags, Track } from './types';
import type { LyricLine } from './lyrics';
import { parseLyrics } from './lyrics';
import { documentLines } from './lyrics-document';
import type { SlotSelection } from './MediaSlotsPanel';
import type { ArtworkReceipt } from './ArtworkImport';
export function tagText(tags: Tags | undefined, key: string): string {
  const value = tags?.[key];
  return Array.isArray(value) ? value.join('; ') : value == null ? '' : String(value);
}
// Send changed fields only. The backend preserves raw multivalue tags and merges
// the immutable base proposal; displaying a value must never rewrite it.
export function changedTags(original: Tags, draft: Record<string, string>, previous?: Tags): Record<string, string> {
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== tagText(previous && key in previous ? previous : original, key)));
}

export type EditorDraft = {
  tags: Record<string, string>; lrc: string; lines?: LyricLine[];
  metadata: Record<string, string[]>; cover: string;
  slotSelection?: SlotSelection; artworkReceipt?: ArtworkReceipt;
};

// The effective saved proposal is the baseline, even when it differs from the
// audio file. Cover is a local overlay: the server keeps the inherited proposal
// image private and merges it by base_proposal_id when there is no replacement.
export function editorBaseline(track: Track, keys: string[]): EditorDraft {
  const proposal = track.proposal?.input_sha256 === track.sha256 ? track.proposal : null;
  return {
    tags: Object.fromEntries(keys.map(key => [key, tagText(proposal?.tags && key in proposal.tags ? proposal.tags : track.tags, key)])),
    lrc: proposal?.lrc ?? track.lrc ?? '', lines: documentLines(track.lyrics_document),
    metadata: track.lyrics_document?.metadata || {}, cover: '', slotSelection: proposal?.slot_selection,
  };
}

function sameMetadata(a: Record<string, string[]>, b: Record<string, string[]>) {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key =>
    b[key]?.length === a[key].length && a[key].every((value, index) => value === b[key][index]));
}

export function lyricsDraftChanged(base: EditorDraft, draft: EditorDraft): boolean {
  if (!sameMetadata(base.metadata, draft.metadata)) return true;
  if (base.lines && draft.lines) {
    return base.lines.length !== draft.lines.length || base.lines.some((line, index) => {
      const next = draft.lines![index];
      return line.id !== next.id || line.text !== next.text || line.time !== next.time
        || !!line.wordsLocked !== !!next.wordsLocked || !!line.timingLocked !== !!next.timingLocked;
    });
  }
  if (base.lrc === draft.lrc) return false;
  // Raw text is still pending parsing in the structured editor. Do not infer
  // durable IDs or silently discard unknown metadata from that draft.
  if (base.lines) return true;
  try {
    const before = parseLyrics(base.lrc), after = parseLyrics(draft.lrc);
    return !sameMetadata(before.metadata, after.metadata) || before.lines.length !== after.lines.length
      || before.lines.some((line, index) => line.text !== after.lines[index].text || line.time !== after.lines[index].time);
  } catch { return true; }
}

export function editorDraftChanged(base: EditorDraft, draft: EditorDraft): boolean {
  const keys = Object.keys(base.tags);
  if (keys.length !== Object.keys(draft.tags).length || keys.some(key => base.tags[key] !== draft.tags[key])
      || lyricsDraftChanged(base, draft) || base.cover !== draft.cover) return true;
  const a = base.slotSelection, b = draft.slotSelection;
  if (a?.inventory_sha256 !== b?.inventory_sha256 || (a?.cover_slot_id ?? null) !== (b?.cover_slot_id ?? null)
      || (a?.lyrics_slot_id ?? null) !== (b?.lyrics_slot_id ?? null)) return true;
  return base.artworkReceipt?.preview_id !== draft.artworkReceipt?.preview_id
    || !!base.artworkReceipt?.confirm_smaller_cover !== !!draft.artworkReceipt?.confirm_smaller_cover;
}
