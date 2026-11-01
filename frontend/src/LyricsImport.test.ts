import { describe, expect, it } from 'vitest';
import { decodeLyrics } from './LyricsImport';

describe('explicit lyric import encoding preview', () => {
  function buffer(bytes: number[]): ArrayBuffer { return Uint8Array.from(bytes).buffer as ArrayBuffer; }
  it('accepts UTF-8 BOM without changing occurrence/offset syntax', () => {
    const text = '[offset:500]\n[00:01][00:02]Припев';
    const bytes = new TextEncoder().encode('\ufeff' + text);
    expect(decodeLyrics(bytes.buffer as ArrayBuffer, 'utf-8')).toBe(text);
  });
  it('requires explicit Windows-1251 choice instead of guessing', () => {
    const bytes = buffer([0x5b, 0x30, 0x30, 0x3a, 0x30, 0x31, 0x5d, 0xcf, 0xf0, 0xe8, 0xef, 0xe5, 0xe2]);
    expect(() => decodeLyrics(bytes, 'utf-8')).toThrow();
    expect(decodeLyrics(bytes, 'windows-1251')).toBe('[00:01]Припев');
  });
  it('previews both UTF-16 byte orders explicitly and rejects nulls/oversize', () => {
    expect(decodeLyrics(buffer([0xff, 0xfe, 0x41, 0, 0x31, 4]), 'utf-16le')).toBe('Aб');
    expect(decodeLyrics(buffer([0xfe, 0xff, 0, 0x41, 4, 0x31]), 'utf-16be')).toBe('Aб');
    expect(() => decodeLyrics(buffer([0x41, 0]), 'utf-8')).toThrow('нулевые');
    expect(() => decodeLyrics(new ArrayBuffer(2 * 1024 * 1024 + 1), 'utf-8')).toThrow('2 МиБ');
  });
});
