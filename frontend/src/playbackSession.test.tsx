import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { clearPlaybackSession, parsePlaybackSession, playbackSessionFor, playbackSessionKey, readPlaybackSession, resolvePlaybackSession, restorePlaybackSession, savePlaybackSession } from './playbackSession';
import type { CueTrack, Track } from './types';

vi.mock('./PwaStatus', () => ({ default: () => null }));
const id = '00000000-0000-0000-0000-000000000002';
const revision = 'a'.repeat(64);
const track: Track = { id, revision, sha256: revision, relative_path: 'private/track.wav', format: 'wav', duration: 30, tags: { title: 'Private track title' }, has_art: true, status: 'indexed', can_write: true };
const context = { search: 'трек', filter: 'published' };
const cue: CueTrack = { number: 3, file_index: 1, title: 'Private CUE title', performer: 'Private artist', available: true, start_seconds: 120, end_seconds: 180 };
const sheet: Track = { ...track, format: 'cue', duration: undefined, cue: { tracks: [cue], files: [{ declared_name: 'private-album.flac' }], warnings: [], errors: [] } };

function storage() {
  const values = new Map<string, string>();
  const store = { getItem: vi.fn((key: string) => values.get(key) ?? null), setItem: vi.fn((key: string, value: string) => { values.set(key, value); }), removeItem: vi.fn((key: string) => { values.delete(key); }) };
  vi.stubGlobal('sessionStorage', store);
  return store;
}
afterEach(() => vi.unstubAllGlobals());

describe('player session navigation pointer', () => {
  it('persists only the track identity, position and catalog context', () => {
    const store = storage();
    const saved = playbackSessionFor(track, 12.5, context)!;
    savePlaybackSession(saved);
    expect(readPlaybackSession()).toEqual(saved);
    const raw = store.setItem.mock.calls[0][1];
    expect(JSON.parse(raw)).toEqual({ version: 1, trackId: id, revision, position: 12.5, search: 'трек', filter: 'published' });
    for (const privateValue of ['Private track title', 'private/track.wav', '/api/', 'tags', 'lyrics', 'csrf', 'autoPlay']) expect(raw).not.toContain(privateValue);
  });

  it('drops injected metadata and stores only a CUE identity, without file paths or titles', () => {
    const saved = playbackSessionFor(sheet, 145, context, cue)!;
    const parsed = parsePlaybackSession(JSON.stringify({ ...saved, track: sheet, audio: '/api/private-audio', csrf: 'secret' }))!;
    expect(parsed).toEqual(saved);
    expect(parsed.cue).toEqual({ number: 3, fileIndex: 1, start: 120, end: 180 });
    expect(JSON.stringify(parsed)).not.toMatch(/Private|private-album|csrf|audio/);
  });

  it('rejects malformed, oversized and non-finite browser state', () => {
    const valid = playbackSessionFor(track, 12, context)!;
    for (const raw of [null, 'null', 'broken', '[]', 'x'.repeat(4097)]) expect(parsePlaybackSession(raw)).toBeNull();
    for (const value of [{ ...valid, version: 2 }, { ...valid, trackId: '../secret' }, { ...valid, revision: 'invalid' },
      { ...valid, trackId: 'fixture-002\n' }, { ...valid, trackId: 'fixture/002' }, { ...valid, trackId: 'x'.repeat(201) },
      { ...valid, position: -1 }, { ...valid, position: null }, { ...valid, position: 1e99 }, { ...valid, filter: 'invalid' },
      { ...valid, cue: { number: 0, fileIndex: 0, start: 1 } }, { ...valid, cue: { number: 1, fileIndex: 0, start: 10, end: 9 } }]) {
      expect(parsePlaybackSession(JSON.stringify(value))).toBeNull();
    }
    expect(playbackSessionFor(track, Infinity, context)).toBeNull();
    expect(parsePlaybackSession(JSON.stringify({ ...valid, search: 'x'.repeat(300) }))?.search).toHaveLength(200);
  });

  it('clears only its own key and tolerates unavailable storage', () => {
    const store = storage(); store.setItem('phonoteka-view-v1', 'view');
    savePlaybackSession(playbackSessionFor(track, 3, context)!); clearPlaybackSession();
    expect(readPlaybackSession()).toBeNull(); expect(store.getItem('phonoteka-view-v1')).toBe('view');
    expect(store.removeItem).toHaveBeenCalledWith(playbackSessionKey);
    vi.stubGlobal('sessionStorage', { getItem() { throw new Error('disabled'); }, setItem() { throw new Error('full'); }, removeItem() { throw new Error('disabled'); } });
    expect(readPlaybackSession()).toBeNull();
    expect(() => { savePlaybackSession(playbackSessionFor(track, 3, context)!); clearPlaybackSession(); }).not.toThrow();
  });
});

describe('fresh authenticated playback restore', () => {
  it.each(['audit_errors', 'audit_warnings', 'audit_unchecked', 'audit_passed'])('restores paused playback inside the %s filtered catalog', async filter => {
    storage();
    const auditContext = { search: 'проверка', filter };
    savePlaybackSession(playbackSessionFor(track, 12.5, auditContext)!);
    const saved = readPlaybackSession()!;
    const load = vi.fn(async () => track);
    expect(await restorePlaybackSession(saved, load, () => true)).toEqual({ track, cue: undefined, initialPosition: 12.5, ...auditContext, autoPlay: false });
    expect(load).toHaveBeenCalledWith(track.id);
  });

  it.each([
    { name: 'ordinary track', current: { ...track, id: 'fixture-002' }, item: undefined },
    { name: 'CUE composition', current: { ...sheet, id: 'fixture-cue' }, item: cue },
  ])('persists and restores the opaque fixture identity for a $name', async ({ current, item }) => {
    const store = storage(); const position = item ? 145 : 12.5;
    savePlaybackSession(playbackSessionFor(current, position, context, item)!);
    const saved = readPlaybackSession()!;
    expect(saved.trackId).toBe(current.id);
    const load = vi.fn(async () => current);
    expect(await restorePlaybackSession(saved, load, () => true)).toMatchObject({ track: current, initialPosition: position, autoPlay: false });
    expect(load).toHaveBeenCalledTimes(1); expect(load).toHaveBeenCalledWith(current.id);
    expect(store.setItem.mock.calls[0][1]).not.toContain(current.relative_path);
  });

  it('uses the fresh server record and restores paused with independent catalog context', async () => {
    const saved = playbackSessionFor(track, 12.5, context)!;
    const fresh = { ...track, tags: { title: 'New server title' } };
    const load = vi.fn(async () => fresh);
    const result = await restorePlaybackSession(saved, load, () => true);
    expect(load).toHaveBeenCalledTimes(1); expect(load).toHaveBeenCalledWith(id);
    expect(result).toEqual({ track: fresh, cue: undefined, initialPosition: 12.5, ...context, autoPlay: false });
    expect(result?.track).toBe(fresh);
    expect(result).not.toHaveProperty('page'); expect(result).not.toHaveProperty('selectedId'); expect(result).not.toHaveProperty('scroll');
  });

  it('clamps ordinary and CUE positions to the freshly verified media window', () => {
    expect(resolvePlaybackSession(playbackSessionFor(track, 50, context)!, track)?.initialPosition).toBe(30);
    const saved = playbackSessionFor(sheet, 145.125, context, cue)!;
    expect(resolvePlaybackSession(saved, sheet)).toMatchObject({ cue, initialPosition: 145.125, autoPlay: false });
    expect(resolvePlaybackSession({ ...saved, position: 90 }, sheet)?.initialPosition).toBe(120);
    expect(resolvePlaybackSession({ ...saved, position: 190 }, sheet)?.initialPosition).toBe(180);
  });

  it('discards changed revisions, unavailable files and changed CUE identity or bounds', () => {
    const saved = playbackSessionFor(track, 12, context)!;
    for (const changed of [{ ...track, sha256: 'b'.repeat(64) }, { ...track, id: '00000000-0000-0000-0000-000000000003' },
      { ...track, status: 'missing' }, { ...track, status: 'superseded' }, { ...track, input_error: { code: 'missing', message: 'missing' } }, sheet]) {
      expect(resolvePlaybackSession(saved, changed)).toBeNull();
    }
    const cueSaved = playbackSessionFor(sheet, 145, context, cue)!;
    for (const changed of [{ ...cue, available: false }, { ...cue, number: 4 }, { ...cue, file_index: 2 }, { ...cue, start_seconds: 121 }, { ...cue, end_seconds: 181 }]) {
      expect(resolvePlaybackSession(cueSaved, { ...sheet, cue: { ...sheet.cue!, tracks: [changed] } })).toBeNull();
    }
  });

  it('ignores a superseded response and never starts a lookup after authentication is lost', async () => {
    let generation = 1; let authenticated = true; let release!: (value: Track) => void;
    const load = vi.fn(() => new Promise<Track>(resolve => { release = resolve; }));
    const pending = restorePlaybackSession(playbackSessionFor(track, 12, context)!, load, () => authenticated && generation === 1);
    generation = 2; release(track);
    expect(await pending).toBeNull(); expect(load).toHaveBeenCalledTimes(1);
    authenticated = false;
    expect(await restorePlaybackSession(playbackSessionFor(track, 12, context)!, load, () => authenticated)).toBeNull();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('allows a fresh effect replay to reuse the saved pointer while the disposed lookup is still pending', async () => {
    const saved = playbackSessionFor(track, 12.5, context)!;
    const release: Array<(value: Track) => void> = [];
    const load = vi.fn(() => new Promise<Track>(resolve => { release.push(resolve); }));
    let generation = 1;
    const disposed = restorePlaybackSession(saved, load, () => generation === 1);
    generation = 2;
    const replay = restorePlaybackSession(saved, load, () => generation === 2);
    release[0]({ ...track, tags: { title: 'Disposed response' } });
    release[1](track);
    expect(await disposed).toBeNull();
    expect(await replay).toMatchObject({ track, initialPosition: 12.5, autoPlay: false });
    expect(load).toHaveBeenCalledTimes(2);
    expect(saved).toEqual(playbackSessionFor(track, 12.5, context));
  });
});

describe('App playback restore authentication boundary', () => {
  it('does not render persisted player metadata or an audio source before the session and fresh track are verified', () => {
    const store = storage();
    store.setItem(playbackSessionKey, JSON.stringify({ ...playbackSessionFor(track, 12, context)!, track, src: '/api/private-audio' }));
    const markup = renderToStaticMarkup(<App />);
    expect(markup).toContain('Подключаемся к фонотеке');
    expect(markup).not.toContain('<audio'); expect(markup).not.toContain('Private track title');
    expect(markup).not.toContain('/api/private-audio'); expect(markup).not.toContain('dock-player');
  });
});
