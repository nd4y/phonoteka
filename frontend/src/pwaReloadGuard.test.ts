import { describe, expect, it, vi } from 'vitest';
import { createPwaReloadGuard } from './pwaReloadGuard';

const unloadEvent = () => ({ preventDefault: vi.fn(), returnValue: 'not-blocked' });

describe('approved PWA reload and unsaved drafts', () => {
  it('keeps the native warning for an ordinary dirty F5 or navigation', () => {
    const guard = createPwaReloadGuard(), event = unloadEvent();
    guard.beforeUnload(event, true);
    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(event.returnValue).toBe('');
  });

  it('saves the latest state before permitting exactly one confirmed reload without a second prompt', () => {
    const guard = createPwaReloadGuard(), duringSave = unloadEvent();
    const saved: number[] = []; let playbackTime = 12;
    playbackTime = 14;
    guard.prepareReload(() => {
      guard.beforeUnload(duringSave, true);
      saved.push(playbackTime);
    });
    expect(duringSave.preventDefault).toHaveBeenCalledOnce();
    expect(saved).toEqual([14]);
    const approved = unloadEvent(); guard.beforeUnload(approved, true);
    expect(approved.preventDefault).not.toHaveBeenCalled();
    expect(approved.returnValue).toBe('not-blocked');
    const laterF5 = unloadEvent(); guard.beforeUnload(laterF5, true);
    expect(laterF5.preventDefault).toHaveBeenCalledOnce();
  });

  it('does not authorize navigation when saving state fails', () => {
    const guard = createPwaReloadGuard();
    guard.prepareReload(() => {});
    expect(() => guard.prepareReload(() => { throw new Error('state not saved'); })).toThrow('state not saved');
    const event = unloadEvent(); guard.beforeUnload(event, true);
    expect(event.preventDefault).toHaveBeenCalledOnce();
  });

  it('restores draft protection after activation failure without clearing drafts', () => {
    const guard = createPwaReloadGuard();
    guard.prepareReload(() => {});
    guard.reset();
    const event = unloadEvent(); guard.beforeUnload(event, true);
    expect(event.preventDefault).toHaveBeenCalledOnce();
    const clean = unloadEvent(); guard.beforeUnload(clean, false);
    expect(clean.preventDefault).not.toHaveBeenCalled();
  });
});
