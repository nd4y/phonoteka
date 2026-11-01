import { describe, expect, it } from 'vitest';
import { compareTiming, timingDelta } from './timing';
import type { AlignmentTimingLine } from './timing';

const line = (original: number | null, proposed: number | null, text = 'Та же строка', state = 'aligned'): AlignmentTimingLine => ({ original_ms: original, start_ms: proposed, text, state });

describe('existing LRC shift and regional drift review', () => {
  it('recognizes a common shift without changing words, ordering or source timestamps', () => {
    const original = [line(1000, 1500, 'Первый припев'), line(30000, 30520, 'Куплет'), line(60000, 60480, 'Второй припев')];
    const checkpoint = JSON.stringify(original);
    const result = compareTiming(original, 100);
    expect(result.pattern).toBe('global_shift');
    expect(result.medianOffsetMs).toBe(500);
    expect(result.globalShiftSuggestionMs).toBe(500);
    expect(result.outlierCount).toBe(0);
    expect(result.residualP95Ms).toBe(20);
    expect(JSON.stringify(original)).toBe(checkpoint);
    expect(result.lines.map(row => row.text)).toEqual(['Первый припев', 'Куплет', 'Второй припев']);
  });
  it('distinguishes accumulated drift across start, middle and end from a global offset', () => {
    const data = [0, 20000, 40000, 60000, 80000, 100000].map((stamp, index) => line(stamp, stamp + index * 400));
    const result = compareTiming(data, 120);
    expect(result.pattern).toBe('local_changes');
    expect(result.globalShiftSuggestionMs).toBeNull();
    expect(result.regions.map(region => region.count)).toEqual([2, 2, 2]);
    expect(result.regions.map(region => region.medianDeltaMs)).toEqual([200, 1000, 1800]);
    expect(result.regionalDriftMs).toBe(1600);
  });
  it('flags an isolated local anomaly robustly instead of shifting the whole document', () => {
    const data = [0, 20000, 40000, 60000, 80000, 100000, 120000].map((stamp, index) => line(stamp, stamp + (index === 3 ? 5000 : 500)));
    const result = compareTiming(data, 150);
    expect(result.medianOffsetMs).toBe(500);
    expect(result.pattern).toBe('local_changes');
    expect(result.outlierCount).toBe(1);
    expect(result.lines[3].outlier).toBe(true);
    expect(result.lines[3].residualMs).toBe(4500);
    expect(result.globalShiftSuggestionMs).toBeNull();
  });
  it('keeps missing anchors and new ASR text explicitly unknown without interpolation', () => {
    const data = [line(1000, null, 'Нет якоря', 'unaligned'), line(null, 9000, 'Новый ASR текст'), line(null, null, 'Нет времени', 'unaligned')];
    const result = compareTiming(data, 100);
    expect(result.pattern).toBe('unknown');
    expect(result.compared).toBe(0);
    expect(result.medianOffsetMs).toBeNull();
    expect(result.regionalDriftMs).toBeNull();
    expect(result.globalShiftSuggestionMs).toBeNull();
    expect(result.lines.map(row => row.deltaMs)).toEqual([null, null, null]);
    expect(result.lines[1].proposedMs).toBe(9000);
  });
  it('does not turn sparse, stale, nonmonotonic or invalid timing into a proposed document shift', () => {
    expect(compareTiming([line(1000, 1500)], 100).pattern).toBe('unknown');
    const valid = [line(1000, 1500), line(10000, 10500), line(20000, 20500)];
    expect(compareTiming(valid, 100, true).pattern).toBe('unknown');
    expect(compareTiming(valid, 100, true).globalShiftSuggestionMs).toBeNull();
    const reversed = compareTiming([line(1000, 3000), line(2000, 1000), line(3000, 4000)], 100);
    expect(reversed.monotonic).toBe(false);
    expect(reversed.pattern).toBe('unknown');
    const invalid = compareTiming([line(1000, NaN), line(2000, -500), line(3000, 200000)], 100);
    expect(invalid.compared).toBe(0);
    expect(invalid.lines.map(row => row.issue)).toEqual(['invalid_time', 'invalid_time', 'outside_audio']);
  });
  it('retains repeated occurrences and equal timestamps as independent comparisons', () => {
    const result = compareTiming([line(1000, 500), line(1000, 500), line(4000, 3500)], 10);
    expect(result.pattern).toBe('global_shift');
    expect(result.globalShiftSuggestionMs).toBe(-500);
    expect(result.lines.map(row => row.index)).toEqual([0, 1, 2]);
    expect(result.monotonic).toBe(true);
    expect(result.regionalDriftMs).toBeNull();
  });
  it('does not offer a whole-document shift when some lines have no anchors', () => {
    const result = compareTiming([line(1000, 1500), line(10000, 10500), line(20000, 20500), line(30000, null, 'Не сопоставлено', 'unaligned')], 100);
    expect(result.pattern).toBe('global_shift');
    expect(result.unpaired).toBe(1);
    expect(result.globalShiftSuggestionMs).toBeNull();
    expect(timingDelta(result.medianOffsetMs)).toBe('+0.500 с');
    expect(timingDelta(null)).toBe('Неизвестно');
  });
});
