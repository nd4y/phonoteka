import { Children, isValidElement } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { PublishedTrackSearch, TrackFilters } from './TrackFilters';

function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...Children.toArray(node.props.children as ReactNode).flatMap(elements)];
}

describe('published collection search', () => {
  it('keeps an inherited query visible and clears it on Ready with the actual page-reset callback', () => {
    let search = '002', page = 7;
    const props = () => ({ search, onSearch: (value: string) => { search = value; }, onPage: (value: number) => { page = value; } });
    const ready = PublishedTrackSearch(props());
    const markup = renderToStaticMarkup(ready);
    expect(markup).toContain('aria-label="Поиск по готовой коллекции"');
    expect(markup).toContain('value="002"');
    expect(markup).toContain('aria-label="Очистить поиск"');
    expect(markup).not.toContain('<select');
    expect(ready.props.filter).toBe('published');

    const clear = elements(TrackFilters(ready.props)).find(node => node.props['aria-label'] === 'Очистить поиск');
    expect(clear).toBeDefined();
    (clear!.props.onClick as () => void)();
    expect(search).toBe('');
    expect(page).toBe(1);
    const cleared = PublishedTrackSearch(props());
    expect(cleared.props.filter).toBe('published');
    expect(renderToStaticMarkup(cleared)).toContain('value=""');
    expect(renderToStaticMarkup(cleared)).not.toContain('aria-label="Очистить поиск"');
  });

  it('resets pagination for a new Ready query without exposing a nonpublished filter', () => {
    const onSearch = vi.fn(), onPage = vi.fn();
    const ready = PublishedTrackSearch({ search: '', onSearch, onPage });
    const input = elements(TrackFilters(ready.props)).find(node => node.type === 'input');
    (input!.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: '001' } });
    expect(onSearch).toHaveBeenCalledTimes(1);
    expect(onSearch).toHaveBeenCalledWith('001');
    expect(onPage).toHaveBeenCalledTimes(1);
    expect(onPage).toHaveBeenCalledWith(1);
    expect(ready.props.filter).toBe('published');
    expect(renderToStaticMarkup(ready)).not.toContain('Фильтр треков');
  });

  it('preserves the normal and compact library/editor search and filter controls', () => {
    const onSearch = vi.fn(), onFilter = vi.fn();
    const props = { search: 'Би-2', onSearch, filter: 'missing_art', onFilter };
    const normal = TrackFilters(props), compact = TrackFilters({ ...props, compact: true });
    for (const tree of [normal, compact]) {
      const markup = renderToStaticMarkup(tree);
      expect(markup).toContain('aria-label="Поиск по коллекции"');
      expect(markup).toContain('value="Би-2"');
      expect(markup).toContain('<select aria-label="Фильтр треков"');
      expect(markup).toMatch(/<option(?=[^>]*value="missing_art")(?=[^>]*selected="")[^>]*>/);
      expect(markup).toContain('aria-label="Очистить поиск"');
    }
    expect(renderToStaticMarkup(compact)).toContain('class="filters compact-filters"');
    const select = elements(normal).find(node => node.type === 'select');
    (select!.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: 'cue' } });
    expect(onFilter).toHaveBeenCalledTimes(1);
    expect(onFilter).toHaveBeenCalledWith('cue');
    expect(onSearch).not.toHaveBeenCalled();
  });
});
