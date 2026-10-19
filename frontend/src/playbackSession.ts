import type { CueTrack, Track } from './types';

export const playbackSessionKey = 'phonoteka-playback-session-v1';
const filters = ['all', 'cue', 'review', 'missing_lyrics', 'missing_art', 'published', 'audit_errors', 'audit_warnings', 'audit_unchecked', 'audit_passed'];
type CueIdentity = { number: number; fileIndex: number; start: number; end?: number };
export type PlaybackSession = {
  version: 1; trackId: string; revision: string; position: number;
  search: string; filter: string; cue?: CueIdentity;
};
export type RestoredPlayback = { track: Track; cue?: CueTrack; initialPosition: number; search: string; filter: string; autoPlay: false };
const finitePosition = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
const trackIdentity = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 200
  && /^[A-Za-z0-9]/.test(value) && !/[^A-Za-z0-9_-]/.test(value);

// Navigation pointers only. Never serialize Track, URLs, tags, lyrics or paths.
export function parsePlaybackSession(raw: string | null): PlaybackSession | null {
  try {
    if (!raw || raw.length > 4096) return null;
    const value = JSON.parse(raw);
    if (!value || value.version !== 1 || !trackIdentity(value.trackId)
      || typeof value.revision !== 'string' || value.revision.length !== 64 || !/^[0-9a-f]{64}$/i.test(value.revision) || !finitePosition(value.position)
      || typeof value.search !== 'string' || !filters.includes(value.filter)) return null;
    let cue: CueIdentity | undefined;
    if (value.cue !== undefined) {
      const item = value.cue;
      if (!item || !Number.isSafeInteger(item.number) || item.number < 1 || !Number.isSafeInteger(item.fileIndex) || item.fileIndex < 0
        || !finitePosition(item.start) || item.end !== undefined && (!finitePosition(item.end) || item.end < item.start)) return null;
      cue = { number: item.number, fileIndex: item.fileIndex, start: item.start, ...(item.end === undefined ? {} : { end: item.end }) };
    }
    return { version: 1, trackId: value.trackId, revision: value.revision, position: value.position,
      search: value.search.slice(0, 200), filter: value.filter, ...(cue ? { cue } : {}) };
  } catch { return null; }
}

export function playbackSessionFor(track: Track, position: number, context: { search: string; filter: string }, cue?: CueTrack): PlaybackSession | null {
  return parsePlaybackSession(JSON.stringify({ version: 1, trackId: track.id, revision: track.sha256, position, search: context.search, filter: context.filter,
    ...(cue ? { cue: { number: cue.number, fileIndex: cue.file_index, start: cue.start_seconds, end: cue.end_seconds } } : {}) }));
}

export function readPlaybackSession(): PlaybackSession | null {
  try { return parsePlaybackSession(sessionStorage.getItem(playbackSessionKey)); } catch { return null; }
}
export function savePlaybackSession(value: PlaybackSession) {
  const checked = parsePlaybackSession(JSON.stringify(value));
  if (!checked) return;
  try { sessionStorage.setItem(playbackSessionKey, JSON.stringify(checked)); } catch { /* Playback works with disabled/full storage. */ }
}
export function clearPlaybackSession() {
  try { sessionStorage.removeItem(playbackSessionKey); } catch { /* No persisted navigation is required for playback. */ }
}

export function resolvePlaybackSession(saved: PlaybackSession, track: Track): RestoredPlayback | null {
  if (track.id !== saved.trackId || track.sha256 !== saved.revision || track.input_error || ['missing', 'superseded'].includes(track.status)) return null;
  let cue: CueTrack | undefined;
  if (saved.cue) {
    if (track.format !== 'cue') return null;
    cue = track.cue?.tracks.find(item => item.number === saved.cue!.number && item.available
      && item.file_index === saved.cue!.fileIndex && item.start_seconds === saved.cue!.start && item.end_seconds === saved.cue!.end);
    if (!cue) return null;
  } else if (track.format === 'cue') return null;
  const start = cue?.start_seconds || 0;
  const end = cue?.end_seconds ?? track.duration;
  const position = Math.max(start, end !== undefined && Number.isFinite(end) && end >= start ? Math.min(saved.position, end) : saved.position);
  return { track, cue, initialPosition: position, search: saved.search, filter: saved.filter, autoPlay: false };
}

// The same request-generation guard is checked before and after the fresh API
// lookup. A closed/replaced player or ended session cannot be resurrected.
export async function restorePlaybackSession(saved: PlaybackSession, load: (id: string) => Promise<Track>, isCurrent: () => boolean): Promise<RestoredPlayback | null> {
  if (!isCurrent()) return null;
  const track = await load(saved.trackId);
  return isCurrent() ? resolvePlaybackSession(saved, track) : null;
}
