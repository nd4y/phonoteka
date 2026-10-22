import { describe, expect, it } from 'vitest';
import { alignmentStatus, instrumentalTextConflict } from './alignmentStatus';
import type { Analysis, Track } from './types';

describe('ASR result explanation', () => {
  const analysis = (words: unknown[], lines: unknown[]) => ({asr:{words},alignment:{lines,coverage:0}} as unknown as Analysis);
  it('distinguishes no ASR words from a broken timing algorithm', () => {
    expect(alignmentStatus(analysis([], [{start_ms:null,reason:'independent_anchor_missing'}]))?.code).toBe('no_recognized_words');
  });
  it('distinguishes recognized words without complete line anchors', () => {
    expect(alignmentStatus(analysis([{probability:.9},{probability:.1}], [{start_ms:null,reason:'independent_anchor_missing'}]))?.code).toBe('no_independent_anchors');
  });
  it('does not call full timing coverage accepted', () => {
    expect(alignmentStatus(analysis([{probability:.9}], [{start_ms:1000,reason:'requires_listening'}]))?.code).toBe('requires_listening');
  });
  it('warns about words proposed for an instrumental title without classifying the audio', () => {
    expect(instrumentalTextConflict({tags:{title:['Example (Instrumental)']},proposal:{lrc:'words'}} as Track)).toBe(true);
    expect(instrumentalTextConflict({tags:{title:'Example'},proposal:{lrc:'words'}} as Track)).toBe(false);
    expect(instrumentalTextConflict({tags:{title:'Example (Instrumental)'}} as Track)).toBe(false);
    expect(instrumentalTextConflict({tags:{title:'Пример (Инструментал)'},lyrics:'words'} as Track)).toBe(true);
  });
});
