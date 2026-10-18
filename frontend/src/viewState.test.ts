import { describe, expect, it } from 'vitest';
import { emptyView, parseView } from './viewState';

describe('reload view state', () => {
  it('restores an editor and a filtered catalog beyond the first page', () => {
    const state = { page: 'lyrics', query: 'Би-2 %_', filter: 'missing_lyrics', pageNumber: 9, selectedId: 'track-42', scroll: 1217, queueScroll: 150 };
    expect(parseView(JSON.stringify(state))).toEqual(state);
  });
  it('rejects malformed storage and clamps browser supplied values', () => {
    expect(parseView('null')).toEqual(emptyView);
    expect(parseView('broken')).toEqual(emptyView);
    expect(parseView(JSON.stringify({ page: 'secret', filter: 'invalid', query: 42, selectedId: {}, pageNumber: -2, scroll: -12, queueScroll: 'a' }))).toEqual(emptyView);
    expect(parseView('{"scroll":1e99,"pageNumber":999999}')).toMatchObject({ scroll: 1e7, pageNumber: 100000 });
  });
});
