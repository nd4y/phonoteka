export const playbackRates = [0.5, 0.75, 1, 1.25, 1.5, 2];
export type PlayerPreferences = { volume: number; muted: boolean; rate: number };
export const defaultPreferences: PlayerPreferences = { volume: 0.8, muted: false, rate: 1 };

export function preferences(value: unknown): PlayerPreferences {
  const item = value && typeof value === 'object' ? value as Partial<PlayerPreferences> : {};
  return {
    volume: typeof item.volume === 'number' && Number.isFinite(item.volume) ? Math.max(0, Math.min(1, item.volume)) : defaultPreferences.volume,
    muted: typeof item.muted === 'boolean' ? item.muted : false,
    rate: playbackRates.includes(item.rate!) ? item.rate! : 1,
  };
}

export function playbackWindow(duration: number, start = 0, end?: number) {
  const total = Number.isFinite(duration) && duration > 0 ? duration : 0;
  const from = Number.isFinite(start) ? Math.max(0, Math.min(total, start)) : 0;
  const to = end !== undefined && Number.isFinite(end) ? Math.max(from, Math.min(total, end)) : total;
  return { start: from, end: to, duration: to - from };
}

export function clampPosition(value: number, start: number, end: number) {
  return Number.isFinite(value) ? Math.max(start, Math.min(end, value)) : start;
}

export function playerTime(seconds: number, precise = false) {
  const ms = Math.max(0, Math.round((Number.isFinite(seconds) ? seconds : 0) * 1000));
  const whole = Math.floor(ms / 1000);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}${precise ? '.' + String(ms % 1000).padStart(3, '0') : ''}`;
}

// Accept minutes:seconds or hours:minutes:seconds, including millisecond precision.
export function parsePlayerTime(value: string): number | null {
  const parts = value.trim().replace(',', '.').split(':');
  if (parts.length < 2 || parts.length > 3 || !parts.every((part, index) => (index === parts.length - 1 ? /^\d{1,2}(?:\.\d{1,3})?$/ : /^\d+$/).test(part))) return null;
  const numbers = parts.map(Number);
  if (numbers[numbers.length - 1] >= 60 || (numbers.length === 3 && numbers[1] >= 60)) return null;
  const seconds = numbers.reduce((total, part) => total * 60 + part, 0);
  return Number.isFinite(seconds) ? seconds : null;
}
