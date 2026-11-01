export interface AlignmentTimingLine {
  text: string;
  original_ms: number | null;
  start_ms: number | null;
  state: string;
  reason?: string;
  low_probability_words?: number;
}
export type TimingPattern = 'unknown' | 'stable' | 'global_shift' | 'local_changes';
export interface TimingComparison {
  index: number;
  text: string;
  originalMs: number | null;
  proposedMs: number | null;
  deltaMs: number | null;
  residualMs: number | null;
  outlier: boolean;
  issue: 'original_missing' | 'anchor_missing' | 'invalid_time' | 'outside_audio' | null;
}
export interface TimingRegion {
  name: 'start' | 'middle' | 'end';
  count: number;
  medianDeltaMs: number | null;
}
export interface TimingReport {
  pattern: TimingPattern;
  lines: TimingComparison[];
  compared: number;
  unpaired: number;
  medianOffsetMs: number | null;
  residualP95Ms: number | null;
  outlierThresholdMs: number | null;
  outlierCount: number;
  regions: TimingRegion[];
  regionalDriftMs: number | null;
  globalShiftSuggestionMs: number | null;
  monotonic: boolean;
  stale: boolean;
}

// Review heuristics, not musical quality gates. These describe differences
// between two timestamp sets, never error relative to a human vocal reference.
export const TIMING_REVIEW_RULES = {
  minimumComparisons: 3,
  uniformResidualMs: 300,
  negligibleShiftMs: 100,
  minimumOutlierMs: 800,
  regionalChangeMs: 600,
  minimumRegionCount: 2,
  minimumRegionSpanMs: 10000,
} as const;

function validMs(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && Number.isSafeInteger(value);
}
function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
function p95(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * .95) - 1)];
}

export function compareTiming(source: readonly AlignmentTimingLine[], durationSeconds?: number, stale = false): TimingReport {
  const durationMs = typeof durationSeconds === 'number' && Number.isFinite(durationSeconds) && durationSeconds > 0 ? durationSeconds * 1000 : null;
  const lines: TimingComparison[] = source.map((line, index) => {
    const originalMs = validMs(line.original_ms) ? line.original_ms : null;
    const proposedMs = validMs(line.start_ms) ? line.start_ms : null;
    let issue: TimingComparison['issue'] = null;
    if ((line.original_ms !== null && !validMs(line.original_ms)) || (line.start_ms !== null && !validMs(line.start_ms))) issue = 'invalid_time';
    else if (durationMs !== null && ((originalMs !== null && originalMs > durationMs) || (proposedMs !== null && proposedMs > durationMs))) issue = 'outside_audio';
    else if (originalMs === null) issue = 'original_missing';
    else if (proposedMs === null || !['aligned', 'anchored'].includes(line.state)) issue = 'anchor_missing';
    return { index, text: line.text, originalMs, proposedMs, deltaMs: issue === null ? proposedMs! - originalMs! : null, residualMs: null, outlier: false, issue };
  });
  const paired = lines.filter(line => line.deltaMs !== null);
  const deltas = paired.map(line => line.deltaMs!);
  const offset = deltas.length ? median(deltas) : null;
  const absoluteResiduals = paired.map(line => Math.abs(line.deltaMs! - offset!));
  const mad = absoluteResiduals.length ? median(absoluteResiduals) : null;
  const threshold = mad === null ? null : Math.max(TIMING_REVIEW_RULES.minimumOutlierMs, 3 * 1.4826 * mad);
  for (const line of paired) {
    line.residualMs = line.deltaMs! - offset!;
    line.outlier = paired.length >= TIMING_REVIEW_RULES.minimumComparisons && Math.abs(line.residualMs) > threshold!;
  }
  let monotonic = true;
  for (let i = 1; i < paired.length; i++) {
    if (paired[i].originalMs! < paired[i - 1].originalMs! || paired[i].proposedMs! < paired[i - 1].proposedMs!) monotonic = false;
  }
  const chronological = [...paired].sort((a, b) => a.originalMs! - b.originalMs! || a.index - b.index);
  const first = chronological[0]?.originalMs ?? 0;
  const span = (chronological.at(-1)?.originalMs ?? first) - first;
  const regions: TimingRegion[] = (['start', 'middle', 'end'] as const).map((name, index) => {
    const points = chronological.filter(line => Math.min(2, Math.floor(((line.originalMs! - first) / (span || 1)) * 3)) === index);
    return { name, count: points.length, medianDeltaMs: points.length ? median(points.map(line => line.deltaMs!)) : null };
  });
  const regionalAvailable = span >= TIMING_REVIEW_RULES.minimumRegionSpanMs && regions.every(region => region.count >= TIMING_REVIEW_RULES.minimumRegionCount);
  const regionalDriftMs = regionalAvailable ? regions[2].medianDeltaMs! - regions[0].medianDeltaMs! : null;
  let pattern: TimingPattern = 'unknown';
  if (!stale && monotonic && paired.length >= TIMING_REVIEW_RULES.minimumComparisons) {
    const uniform = absoluteResiduals.every(value => value <= TIMING_REVIEW_RULES.uniformResidualMs);
    pattern = uniform ? (Math.abs(offset!) <= TIMING_REVIEW_RULES.negligibleShiftMs ? 'stable' : 'global_shift') : 'local_changes';
    if (regionalDriftMs !== null && Math.abs(regionalDriftMs) >= TIMING_REVIEW_RULES.regionalChangeMs) pattern = 'local_changes';
  }
  // A shift across the whole document is suggested only when every original
  // line has a usable comparison. Incomplete regions stay a listening task.
  const globalShiftSuggestionMs = pattern === 'global_shift' && paired.length === lines.length ? Math.round(offset!) : null;
  return {
    pattern, lines, compared: paired.length, unpaired: lines.length - paired.length,
    medianOffsetMs: offset, residualP95Ms: absoluteResiduals.length ? p95(absoluteResiduals) : null,
    outlierThresholdMs: threshold, outlierCount: lines.filter(line => line.outlier).length,
    regions, regionalDriftMs, globalShiftSuggestionMs, monotonic, stale,
  };
}

export function timingDelta(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return 'Неизвестно';
  return `${ms > 0 ? '+' : ''}${(ms / 1000).toFixed(3)} с`;
}
