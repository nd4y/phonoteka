import { createHash } from 'node:crypto';
import { readdir, readFile, writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';
import { isPublicAssetPath, pwaRequestKind } from './pwa-policy.mjs';
import { buildServiceWorkerSource } from './pwa-worker.mjs';
import { publicBuildPaths } from './pwa-inventory.mjs';

const dist = fileURLToPath(new URL('../dist/', import.meta.url));

// Build PNG install icons without an image dependency. The waveform remains
// inside the maskable safe area, including circular Android launcher masks.
function pngIcon(size) {
  const pixels = Buffer.alloc((size * 4 + 1) * size);
  const bars = [[142, 226, 28, 60], [192, 176, 28, 160], [242, 126, 28, 260], [292, 176, 28, 160], [342, 226, 28, 60]];
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const sx = (x + .5) * 512 / size, sy = (y + .5) * 512 / size;
    const bar = bars.some(([left, top, width, height]) => {
      const nearX = left + width / 2;
      const nearY = Math.max(top + width / 2, Math.min(sy, top + height - width / 2));
      return (sx - nearX) ** 2 + (sy - nearY) ** 2 <= (width / 2) ** 2;
    });
    const offset = y * (size * 4 + 1) + 1 + x * 4;
    pixels.set(bar ? [163, 78, 34, 255] : [255, 240, 230, 255], offset);
  }
  const crc = bytes => {
    let value = 0xffffffff;
    for (const byte of bytes) {
      value ^= byte;
      for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
    }
    return (value ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const header = Buffer.alloc(4), checksum = Buffer.alloc(4);
    header.writeUInt32BE(data.length); checksum.writeUInt32BE(crc(body));
    return Buffer.concat([header, body, checksum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
}
await mkdir(`${dist}icons`, { recursive: true });
await Promise.all([192, 512].map(size => writeFile(`${dist}icons/icon-${size}.png`, pngIcon(size))));
const files = publicBuildPaths(await readdir(dist, { recursive: true, withFileTypes: true }), dist);
if (!files.includes('index.html') || !files.includes('manifest.webmanifest') || !files.some(path => path.startsWith('assets/') && path.endsWith('.js'))) throw new Error('PWA public build inventory is incomplete');
const hash = createHash('sha256');
for (const path of files) { hash.update(path); hash.update(await readFile(`${dist}${path}`)); }
// Policy changes also invalidate the worker even if application assets do not.
hash.update(isPublicAssetPath.toString()); hash.update(pwaRequestKind.toString());
hash.update(buildServiceWorkerSource.toString());
const version = hash.digest('hex').slice(0, 20);
const worker = buildServiceWorkerSource(version, files.map(path => `/${path}`));
await writeFile(`${dist}sw.js`, worker);
console.log(`PWA shell ${version}: ${files.length} public files; API, audio and credentials excluded`);
