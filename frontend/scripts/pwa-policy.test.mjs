import { describe, expect, it } from 'vitest';
import { isPublicAssetPath, pwaRequestKind } from './pwa-policy.mjs';

const origin = 'https://music.example';
const precache = ['/index.html', '/assets/index-abc.js', '/icons/icon-192.png', '/manifest.webmanifest'];
const request = (path, overrides = {}) => ({ url: `${origin}${path}`, method: 'GET', mode: 'cors', headers: new Headers(), ...overrides });

describe('private data never enters the PWA cache', () => {
  it('only serves a known public shell and build assets', () => {
    expect(pwaRequestKind(request('/', { mode: 'navigate' }), origin, precache)).toBe('navigation');
    expect(pwaRequestKind(request('/index.html', { mode: 'navigate' }), origin, precache)).toBe('navigation');
    expect(pwaRequestKind(request('/assets/index-abc.js'), origin, precache)).toBe('static');
    expect(pwaRequestKind(request('/assets/unknown.js'), origin, precache)).toBeNull();
    expect(pwaRequestKind(request('/manifest.webmanifest'), origin, precache)).toBe('static');
  });
  it('does not intercept APIs, auth, audio, lyrics, events, metrics or private artwork', () => {
    for (const path of ['/api/tracks', '/api/auth/session', '/api/v1/events', '/api/tracks/1/audio', '/api/tracks/1/cover', '/auth/login', '/metrics', '/health', '/music/a.mp3', '/assets/audio.mp3', '/private/lyrics.json']) {
      expect(pwaRequestKind(request(path), origin, [...precache, path])).toBeNull();
      expect(pwaRequestKind(request(path, { mode: 'navigate' }), origin, [...precache, path])).toBeNull();
    }
  });
  it('leaves Range, authorized, mutated, queried and external requests to the network', () => {
    const file = '/assets/index-abc.js';
    expect(pwaRequestKind(request(file, { headers: new Headers({ Range: 'bytes=0-99' }) }), origin, precache)).toBeNull();
    expect(pwaRequestKind(request(file, { headers: new Headers({ Authorization: 'Bearer synthetic-test' }) }), origin, precache)).toBeNull();
    expect(pwaRequestKind(request(file, { method: 'POST' }), origin, precache)).toBeNull();
    expect(pwaRequestKind(request(`${file}?token=private`), origin, precache)).toBeNull();
    expect(pwaRequestKind(request(file, { url: `https://external.example${file}` }), origin, precache)).toBeNull();
    expect(pwaRequestKind(request(file, { url: 'invalid' }), origin, precache)).toBeNull();
  });
  it('rejects accidental private paths in the generated public file inventory', () => {
    expect(isPublicAssetPath('/api/private.js')).toBe(false);
    expect(isPublicAssetPath('/assets/nested/private.js')).toBe(false);
    expect(isPublicAssetPath('/icons/icon-192.png')).toBe(true);
  });
});
