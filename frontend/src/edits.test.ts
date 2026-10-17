import { describe, expect, it } from 'vitest';
import { changedTags, tagText } from './edits';
describe('proposals preserve untouched metadata', () => {
  it('a title edit does not flatten or send the multivalue artist', () => {
    const original = { title: 'Старое', artist: ['Первый', 'Второй'], album: 'Альбом' };
    expect(changedTags(original, { title: 'Новое', artist: tagText(original, 'artist'), album: 'Альбом' })).toEqual({ title: 'Новое' });
  });
  it('inherits unchanged proposal fields and explicitly sends a reverted field', () => {
    expect(changedTags({ title: 'Исходное', artist: 'Артист' }, { title: 'Исходное', artist: 'Артист' }, { title: 'Черновик' })).toEqual({ title: 'Исходное' });
    expect(changedTags({ title: 'Исходное' }, { title: 'Черновик' }, { title: 'Черновик' })).toEqual({});
  });
});
