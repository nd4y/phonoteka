import { describe, expect, it } from 'vitest';
import { formatTime, parseLyrics, parseTime, serializeLyrics, validateLyrics } from './lyrics';
describe('Poweramp line lyrics editing', () => {
  it('expands repeated tags and orders occurrences without merging text', () => {
    const result = parseLyrics('[00:10][00:40]Припев\n[00:20]Куплет');
    expect(result.lines.map(x => [x.time, x.text])).toEqual([[10000, 'Припев'], [20000, 'Куплет'], [40000, 'Припев']]);
    expect(new Set(result.lines.map(x => x.id)).size).toBe(3);
  });
  it('applies a positive offset once, subtracting from audio time', () => {
    const lines = parseLyrics('\uFEFF[offset:+500]\n[00:10.00]Строка').lines;
    expect(lines[0].time).toBe(9500);
    expect(parseLyrics(serializeLyrics(lines)).lines[0].time).toBe(9500);
  });
  it('preserves untimed and empty timed rows', () => {
    const lines = parseLyrics('[00:03.1]Слова\nБез времени\n[00:04.00]').lines;
    expect(lines.map(x => x.time)).toEqual([3100, null, 4000]);
    expect(lines[2].text).toBe('');
  });
  it('rejects ambiguous offsets, malformed and enhanced timestamps', () => {
    expect(() => parseLyrics('[offset:1]\n[offset:2]\n[00:10]а')).toThrow();
    expect(() => parseLyrics('[00:70]а')).toThrow();
    expect(() => parseLyrics('[00:03]<00:04>а')).toThrow();
    expect(() => parseLyrics('[offset:1000]\n[00:00.5]а')).toThrow();
  });
  it('accepts long tracks and equal times, rejects backward or out of duration times', () => {
    expect(parseTime('120:01.5')).toBe(7201500);
    expect(formatTime(7201500)).toBe('120:01.500');
    const lines = [{ id: 'a', time: 1000, text: 'а' }, { id: 'b', time: 1000, text: 'б' }];
    expect(validateLyrics(lines, 2)).toBeNull();
    expect(validateLyrics([{...lines[0],time:3000}], 2)).toContain('длительность');
    expect(validateLyrics([lines[0], {...lines[1],time:500}])).toContain('порядок');
  });
});
