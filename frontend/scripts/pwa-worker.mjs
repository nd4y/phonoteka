import { isPublicAssetPath, pwaRequestKind } from './pwa-policy.mjs';

export function buildServiceWorkerSource(version, files) {
  if (!/^[a-f0-9]{20}$/.test(version) || files.some(path => !isPublicAssetPath(path))) throw new Error('Invalid public PWA inventory');
  return `/* Phonoteka public shell ${version}. Private data is never cached. */
const CACHE_PREFIX = 'phonoteka-shell-';
const CACHE = CACHE_PREFIX + '${version}';
const PRECACHE = ${JSON.stringify(files)};
${isPublicAssetPath.toString()}
${pwaRequestKind.toString()}
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(PRECACHE)));
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const own = (await caches.keys()).filter(key => key.startsWith(CACHE_PREFIX));
    const previous = own.filter(key => key !== CACHE).slice(-1);
    await Promise.all(own.filter(key => key !== CACHE && !previous.includes(key)).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});
self.addEventListener('message', event => {
  if (event.data?.type === 'PHONOTEKA_ACTIVATE') event.waitUntil(self.skipWaiting());
});
async function publicAsset(path, request) {
  const own = (await caches.keys()).filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE).reverse();
  for (const key of [CACHE, ...own]) {
    const hit = await caches.open(key).then(cache => cache.match(path));
    if (hit) return hit;
  }
  return fetch(request);
}
self.addEventListener('fetch', event => {
  const kind = pwaRequestKind(event.request, self.location.origin, PRECACHE);
  if (kind === 'navigation') {
    event.respondWith(fetch(event.request, { cache: 'no-store' }).catch(async () => {
      const shell = await caches.open(CACHE).then(cache => cache.match('/index.html'));
      return shell || Response.error();
    }));
  } else {
    // A tab that stayed open across an update may request an older hashed asset.
    // Look it up only in our public shell caches; never add a runtime response.
    const path = new URL(event.request.url).pathname;
    if (kind === 'static' || pwaRequestKind(event.request, self.location.origin, [path]) === 'static') {
      event.respondWith(publicAsset(path, event.request));
    }
  }
});
`;
}
