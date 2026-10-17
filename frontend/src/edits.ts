import type { Tags } from './types';
export function tagText(tags: Tags | undefined, key: string): string {
  const value = tags?.[key];
  return Array.isArray(value) ? value.join('; ') : value == null ? '' : String(value);
}
// Send changed fields only. The backend preserves raw multivalue tags and merges
// the immutable base proposal; displaying a value must never rewrite it.
export function changedTags(original: Tags, draft: Record<string, string>, previous?: Tags): Record<string, string> {
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== tagText(previous && key in previous ? previous : original, key)));
}
