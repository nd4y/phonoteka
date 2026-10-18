import path from 'node:path';
import { isPublicAssetPath } from './pwa-policy.mjs';

// Dirent.parentPath may retain a trailing separator (notably for the root
// passed to recursive readdir). Never derive a URL by slicing that string.
export function publicBuildPaths(entries, dist, paths = path) {
  return entries.filter(file => file.isFile())
    .map(file => paths.relative(dist, paths.resolve(file.parentPath || file.path, file.name)).split(paths.sep).join('/'))
    .filter(file => isPublicAssetPath(`/${file}`))
    .sort();
}
