import { runInNewContext } from 'node:vm';
import { describe, expect, it, vi } from 'vitest';
import { buildServiceWorkerSource } from './pwa-worker.mjs';

const version = '0123456789abcdefabcd';
const inventory = ['/index.html', '/assets/index-abc.js', '/manifest.webmanifest', '/icons/icon-192.png'];
function fixture() {
  const handlers = new Map();
  const cache = { addAll: vi.fn(async () => undefined), match: vi.fn(async () => new Response('public shell')) };
  const caches = { open: vi.fn(async () => cache), keys: vi.fn(async () => ['foreign-cache', 'phonoteka-shell-old1', 'phonoteka-shell-old2', `phonoteka-shell-${version}`]), delete: vi.fn(async () => true) };
  const self = { addEventListener: (name, handler) => handlers.set(name, handler), location: { origin: 'https://music.example' }, skipWaiting: vi.fn(async () => undefined), clients: { claim: vi.fn(async () => undefined) } };
  const fetch = vi.fn(async () => { throw new TypeError('offline'); });
  runInNewContext(buildServiceWorkerSource(version, inventory), { self, caches, fetch, URL, Response });
  return { handlers, cache, caches, self, fetch };
}

describe('generated worker lifecycle and offline behavior', () => {
  it('preloads only the public inventory, keeps an old shell and leaves other apps alone', async () => {
    const f = fixture();
    await new Promise(resolve => f.handlers.get('install')({ waitUntil: promise => promise.then(resolve) }));
    expect(f.cache.addAll).toHaveBeenCalledWith(inventory);
    expect(f.self.skipWaiting).not.toHaveBeenCalled();
    await new Promise(resolve => f.handlers.get('activate')({ waitUntil: promise => promise.then(resolve) }));
    expect(f.caches.delete).toHaveBeenCalledTimes(1);
    expect(f.caches.delete).toHaveBeenCalledWith('phonoteka-shell-old1');
    expect(f.self.clients.claim).toHaveBeenCalledOnce();
  });
  it('uses the shell for an offline root navigation, without caching network responses', async () => {
    const f = fixture();
    let response;
    f.handlers.get('fetch')({ request: { method: 'GET', mode: 'navigate', headers: new Headers(), url: 'https://music.example/' }, respondWith: promise => { response = promise; } });
    expect(await (await response).text()).toBe('public shell');
    expect(f.fetch).toHaveBeenCalledWith(expect.anything(), { cache: 'no-store' });
    expect(f.cache.match).toHaveBeenCalledWith('/index.html');
    expect(f.cache.addAll).not.toHaveBeenCalled();
  });
  it('does not intercept private requests even while offline', () => {
    const f = fixture();
    const respondWith = vi.fn();
    for (const path of ['/api/auth/session', '/api/tracks/1/audio', '/api/tracks/1/cover', '/api/events', '/metrics']) {
      f.handlers.get('fetch')({ request: { method: 'GET', mode: 'cors', headers: new Headers(), url: `https://music.example${path}` }, respondWith });
    }
    f.handlers.get('fetch')({ request: { method: 'GET', mode: 'cors', headers: new Headers({ range: 'bytes=0-20' }), url: 'https://music.example/assets/index-abc.js' }, respondWith });
    expect(respondWith).not.toHaveBeenCalled();
    expect(f.caches.open).not.toHaveBeenCalled();
  });
  it('activates a waiting update only on the explicit application message', async () => {
    const f = fixture();
    f.handlers.get('message')({ data: { type: 'something-else' } });
    expect(f.self.skipWaiting).not.toHaveBeenCalled();
    await new Promise(resolve => f.handlers.get('message')({ data: { type: 'PHONOTEKA_ACTIVATE' }, waitUntil: promise => promise.then(resolve) }));
    expect(f.self.skipWaiting).toHaveBeenCalledOnce();
    expect(() => buildServiceWorkerSource(version, ['/api/auth/session'])).toThrow();
  });
  it('keeps assets available for another tab that stayed on the previous version', async () => {
    const f = fixture();
    f.caches.open.mockImplementation(async key => ({ match: async path => key === 'phonoteka-shell-old2' && path === '/assets/index-previous.js' ? new Response('previous public asset') : undefined }));
    let response;
    f.handlers.get('fetch')({ request: { method: 'GET', mode: 'cors', headers: new Headers(), url: 'https://music.example/assets/index-previous.js' }, respondWith: promise => { response = promise; } });
    expect(await (await response).text()).toBe('previous public asset');
    expect(f.fetch).not.toHaveBeenCalled();
    expect(f.cache.addAll).not.toHaveBeenCalled();
  });
});
