import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { PwaState } from './pwa';
import PwaStatus from './PwaStatus';

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock('./pwa', () => ({
  usePwa: snapshot,
  activatePwaUpdate: vi.fn(), checkPwaUpdate: vi.fn(), dismissPwaError: vi.fn(), promptPwaInstall: vi.fn(),
}));

const initial: PwaState = {
  supported: true, installed: true, installable: false, offline: false,
  offlineReady: true, updateReady: false, updating: false, error: null,
};
function render(state: Partial<PwaState>) {
  snapshot.mockReturnValue({ ...initial, ...state });
  return renderToStaticMarkup(<PwaStatus showInstall={false} />);
}

describe('PWA update panel lifecycle', () => {
  it('keeps the section and update banner while the accepted waiting worker activates', () => {
    const waiting = render({ updateReady: true });
    const activating = render({ updateReady: false, updating: true });
    for (const markup of [waiting, activating]) {
      expect(markup).toContain('<section class="pwa-status"');
      expect(markup).toContain('<div class="pwa-banner update" role="status">');
      expect(markup).toContain('<strong>Доступно обновление</strong>');
      expect(markup).toContain('<p>Установите, когда закончите текущие правки.</p>');
      expect(markup.match(/<button\b/g)).toHaveLength(1);
    }
    expect(waiting).toMatch(/<button\b(?![^>]*\bdisabled=)[^>]*>Обновить приложение<\/button>/);
    expect(activating).toMatch(/<button\b[^>]*\bdisabled=""[^>]*>Обновляем…<\/button>/);
  });

  it('removes the waiting banner when another tab completed the update without this tab activating it', () => {
    expect(render({ updateReady: true })).toContain('pwa-banner update');
    expect(render({ updateReady: false, updating: false })).toBe('');
  });
});
