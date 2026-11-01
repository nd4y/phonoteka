import { Children, isValidElement } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { AutomationPanel, TOKEN_SCOPES, TokenScopePicker } from './AutomationPanel';
import type { TokenScope } from './AutomationPanel';

function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...Children.toArray(node.props.children as ReactNode).flatMap(elements)];
}

describe('explicit API token capabilities', () => {
  it('offers four independent canonical permissions and defaults to read plus analysis jobs', () => {
    expect(TOKEN_SCOPES.map(option => option.value)).toEqual(['library:read', 'jobs:create', 'files:write', 'publish:write']);
    const markup = renderToStaticMarkup(<AutomationPanel/>);
    expect((markup.match(/checked=""/g) || []).length).toBe(2);
    expect(markup).toContain('Создание задач само по себе не разрешает запись файлов или публикацию');
    expect(markup).not.toContain('jobs:write');
  });

  it.each<TokenScope>(['files:write', 'publish:write'])('does not add other permissions when selecting %s', scope => {
    const onChange = vi.fn();
    const tree = TokenScopePicker({ scopes: ['library:read'], onChange });
    const index = TOKEN_SCOPES.findIndex(option => option.value === scope);
    const input = elements(tree).filter(node => node.type === 'input')[index];
    (input.props.onChange as (event: { target: { checked: boolean } }) => void)({ target: { checked: true } });
    expect(onChange.mock.calls).toEqual([[['library:read', scope]]]);
  });

  it('removes exactly the selected scope and never duplicates an existing one', () => {
    const onChange = vi.fn();
    const selected: TokenScope[] = ['jobs:create', 'files:write', 'publish:write'];
    const tree = TokenScopePicker({ scopes: selected, onChange });
    const files = elements(tree).filter(node => node.type === 'input')[2];
    const toggle = files.props.onChange as (event: { target: { checked: boolean } }) => void;
    toggle({ target: { checked: false } });
    toggle({ target: { checked: true } });
    expect(onChange.mock.calls).toEqual([[['jobs:create', 'publish:write']], [['jobs:create', 'publish:write', 'files:write']]]);
  });
});
