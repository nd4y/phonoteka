import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CollectionOverview, countValue, coverage } from './CollectionOverview';

describe('collection overview meaning', () => {
  it('uses audio files, not CUE documents, as the denominator', () => {
    const html = renderToStaticMarkup(<CollectionOverview counts={{ files: 12, cue_sheets: 2, audio_files: 10, with_core_tags: 5, with_cover: 2, with_lyrics: 0 }}/>);
    expect(html).toContain('50%'); expect(html).toContain('20%'); expect(html).not.toContain('42%');
    expect(html).toContain('не подтверждает'); expect(html).not.toContain('Поврежденные файлы');
  });
  it('does not invent success for missing or impossible counters', () => {
    expect(countValue(undefined)).toBeNull(); expect(countValue(-1)).toBeNull();
    expect(coverage(5, 0)).toBeNull(); expect(coverage(11, 10)).toBeNull(); expect(coverage(NaN,10)).toBeNull();
    const html = renderToStaticMarkup(<CollectionOverview counts={{files: 99}}/>);
    expect(html).not.toContain('<progress'); expect(html).not.toContain('NaN'); expect(html).not.toContain('100%');
  });
});
