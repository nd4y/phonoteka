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
    const track = (values: Partial<Track>): Track => ({id:'test', relative_path:'test.mp3', sha256:'test', revision:'test', format:'mp3', tags:{}, has_art:false, status:'scanned', can_write:false, ...values});
    const proposal = {id:'proposal',input_sha256:'test',tags:{},lrc:'words'};
    expect(instrumentalTextConflict(track({tags:{title:['Example (Instrumental)']},proposal}))).toBe(true);
    expect(instrumentalTextConflict(track({tags:{title:'Example'},proposal}))).toBe(false);
    expect(instrumentalTextConflict(track({tags:{title:'Example (Instrumental)'}}))).toBe(false);
    expect(instrumentalTextConflict(track({tags:{title:'Пример (Инструментал)'},lyrics:'words'}))).toBe(true);
  });
});
