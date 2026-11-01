import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { publicBuildPaths } from './pwa-inventory.mjs';

const entry = (parentPath, name, file = true) => ({ parentPath, name, isFile: () => file });
describe('public build inventory paths', () => {
  it('normalizes trailing separators from recursive Linux readdir', () => {
    const files = [entry('/build/dist/', 'index.html'), entry('/build/dist/', 'manifest.webmanifest'), entry('/build/dist/assets', 'index-abc.js'), entry('/build/dist/icons/', 'icon-192.png'), entry('/build/dist/', 'icons', false)];
    expect(publicBuildPaths(files, '/build/dist/', path.posix)).toEqual(['assets/index-abc.js', 'icons/icon-192.png', 'index.html', 'manifest.webmanifest']);
    expect(publicBuildPaths(files, '/build/dist', path.posix)).toEqual(publicBuildPaths(files, '/build/dist/', path.posix));
  });
  it('supports Windows parent paths and returns portable URL paths', () => {
    const files = [entry('C:\\build\\dist\\', 'index.html'), entry('C:\\build\\dist\\assets\\', 'index-abc.css'), entry('C:\\build\\dist\\icons', 'icon-512.png')];
    expect(publicBuildPaths(files, 'C:\\build\\dist\\', path.win32)).toEqual(['assets/index-abc.css', 'icons/icon-512.png', 'index.html']);
  });
  it('supports the older Dirent.path property and excludes private or outside files', () => {
    const files = [{ path: '/build/dist/', name: 'index.html', isFile: () => true }, entry('/build/dist/api', 'session.json'), entry('/build/private', 'index.html'), entry('/build/dist/assets', 'audio.mp3')];
    expect(publicBuildPaths(files, '/build/dist/', path.posix)).toEqual(['index.html']);
  });
});
