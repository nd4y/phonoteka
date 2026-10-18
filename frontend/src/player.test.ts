import { describe, expect, it } from 'vitest';
import { clampPosition, parsePlayerTime, playbackWindow, playerTime, preferences } from './player';

describe('audio preview boundaries and exact seeking', () => {
  it('clips CUE controls to the selected composition in a shared album file', () => {
    const clip = playbackWindow(3600, 120, 245.533);
    expect(clip.duration).toBeCloseTo(125.533);
    expect(clampPosition(110, clip.start, clip.end)).toBe(120);
    expect(clampPosition(260, clip.start, clip.end)).toBe(245.533);
    expect(clampPosition(NaN, clip.start, clip.end)).toBe(120);
    expect(clampPosition(clip.start + parsePlayerTime('1:05.125')!, clip.start, clip.end)).toBe(185.125);
  });
  it('keeps unknown or changed media duration finite and bounded', () => {
    expect(playbackWindow(Infinity, 100, 120)).toEqual({ start: 0, end: 0, duration: 0 });
    expect(playbackWindow(NaN)).toEqual({ start: 0, end: 0, duration: 0 });
    expect(playbackWindow(100, 120, 90)).toEqual({ start: 100, end: 100, duration: 0 });
    expect(playbackWindow(100, 10, 120)).toEqual({ start: 10, end: 100, duration: 90 });
  });
  it('accepts millisecond times, long files, hours and decimal commas', () => {
    expect(parsePlayerTime('47:19.533')).toBe(2839.533);
    expect(parsePlayerTime('1:02:03,125')).toBe(3723.125);
    expect(parsePlayerTime('120:00')).toBe(7200);
    expect(playerTime(2839.533, true)).toBe('47:19.533');
    expect(playerTime(59.9999, true)).toBe('1:00.000');
  });
  it('rejects malformed or ambiguous time values', () => {
    for (const value of ['', '-1:01', '1:60', '1:60:00', '1:2:3:4', '1e2:03', '1:00.1234', '1:NaN', ' 25 ']) expect(parsePlayerTime(value)).toBeNull();
  });
  it('validates stored preferences without restoring arbitrary playback rates', () => {
    expect(preferences({ volume: 2, muted: true, rate: 2 })).toEqual({ volume: 1, muted: true, rate: 2 });
    expect(preferences({ volume: -1, muted: 'yes', rate: 100 })).toEqual({ volume: 0, muted: false, rate: 1 });
    expect(preferences({ volume: NaN })).toEqual({ volume: 0.8, muted: false, rate: 1 });
    expect(preferences(null)).toEqual({ volume: 0.8, muted: false, rate: 1 });
  });
});
