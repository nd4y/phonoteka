// This allowlist is also embedded verbatim in the generated service worker.
// No runtime response is added to a cache: only the public files produced by Vite.
export function isPublicAssetPath(path) {
  return path === '/' || path === '/index.html' || path === '/manifest.webmanifest'
    || /^\/icons\/icon(?:-192|-512)?\.(?:png|svg)$/.test(path)
    || /^\/assets\/[^/]+\.(?:js|css|woff2?|svg|png|webp|ico)$/.test(path);
}

export function pwaRequestKind(request, origin, precachePaths) {
  let url;
  try { url = new URL(request.url); } catch { return null; }
  if (request.method !== 'GET' || url.origin !== origin || url.search
      || request.headers.has('range') || request.headers.has('authorization')
      || !isPublicAssetPath(url.pathname)) return null;
  if (request.mode === 'navigate') {
    return url.pathname === '/' || url.pathname === '/index.html' ? 'navigation' : null;
  }
  return precachePaths.includes(url.pathname) ? 'static' : null;
}
