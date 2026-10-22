import { describe, expect, it, vi } from 'vitest';
import { acceptDetailNavigation, loadLatestResult, loadOrderedResult } from './detailNavigation';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe('track details across accepted manual navigation', () => {
  it('cannot overwrite a cancelled task with an older in-flight queue snapshot', async () => {
    const sequence = { issued: 0, committed: 0 }, old = deferred<string>(), fresh = deferred<string>();
    const commit = vi.fn(), failed = vi.fn();
    const first = loadOrderedResult(sequence, () => old.promise, commit, failed);
    const second = loadOrderedResult(sequence, () => fresh.promise, commit, failed);
    fresh.resolve('cancelled'); await second;
    old.resolve('running'); await first;
    expect(commit).toHaveBeenCalledExactlyOnceWith('cancelled');
    expect(failed).not.toHaveBeenCalled();
  });
  it('does not starve useful polling snapshots when HTTP is slower than the poll interval', async () => {
    const sequence = { issued: 0, committed: 0 }, old = deferred<string>(), fresh = deferred<string>();
    const commit = vi.fn(), failed = vi.fn();
    const first = loadOrderedResult(sequence, () => old.promise, commit, failed);
    const second = loadOrderedResult(sequence, () => fresh.promise, commit, failed);
    old.resolve('first-progress'); await first;
    expect(commit).toHaveBeenCalledExactlyOnceWith('first-progress');
    fresh.resolve('next-progress'); await second;
    expect(commit.mock.calls.map(call => call[0])).toEqual(['first-progress', 'next-progress']);
  });
  it.each(['logout', 'session expiry'])('ignores an old queue snapshot and failure after %s', async () => {
    const sequence = { issued: 0, committed: 0 }, old = deferred<string>(), other = deferred<string>();
    const commit = vi.fn(), failed = vi.fn();
    const first = loadOrderedResult(sequence, () => old.promise, commit, failed);
    const second = loadOrderedResult(sequence, () => other.promise, commit, failed);
    sequence.committed = ++sequence.issued;
    old.resolve('private-status'); other.reject(new Error('old failure')); await Promise.all([first, second]);
    expect(commit).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled();
  });
  it('does not reopen the editor or replace playback after the owner leaves for Processing', async () => {
    const sequence = { current: 0 }, response = deferred<string>();
    let page = 'library', playback = 'original';
    const commit = vi.fn((track: string) => { page = 'review'; playback = track; });
    const failed = vi.fn(), settled = vi.fn();
    const pending = loadLatestResult(sequence, () => response.promise, commit, failed, settled);
    if (acceptDetailNavigation(sequence, () => true)) page = 'jobs';
    response.resolve('late-track');
    expect(await pending).toBe(false);
    expect(page).toBe('jobs'); expect(playback).toBe('original');
    expect(commit).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled();
    expect(settled).not.toHaveBeenCalled();
  });

  it('keeps a valid pending flow when the owner rejects the discard prompt', async () => {
    const sequence = { current: 0 }, response = deferred<string>();
    const commit = vi.fn(), settled = vi.fn(), approve = vi.fn(() => false);
    const pending = loadLatestResult(sequence, () => response.promise, commit, vi.fn(), settled);
    const before = sequence.current;
    expect(acceptDetailNavigation(sequence, approve)).toBe(false);
    expect(sequence.current).toBe(before); expect(approve).toHaveBeenCalledOnce();
    response.resolve('requested-track');
    expect(await pending).toBe(true);
    expect(commit).toHaveBeenCalledWith('requested-track'); expect(settled).toHaveBeenCalledOnce();
  });

  it('accepts a subsequent track selection and ignores an older response and failure', async () => {
    const sequence = { current: 0 }, old = deferred<string>(), selected = deferred<string>();
    const commit = vi.fn(), failed = vi.fn(), settled = vi.fn();
    const first = loadLatestResult(sequence, () => old.promise, commit, failed, settled);
    acceptDetailNavigation(sequence, () => true);
    const next = loadLatestResult(sequence, () => selected.promise, commit, failed, settled);
    selected.resolve('new-selection'); expect(await next).toBe(true);
    old.reject(new Error('late network failure')); expect(await first).toBe(false);
    expect(commit).toHaveBeenCalledExactlyOnceWith('new-selection');
    expect(failed).not.toHaveBeenCalled(); expect(settled).toHaveBeenCalledOnce();
  });

  it.each(['logout', 'session expiry'])('ignores pending details after %s invalidates their generation', async () => {
    const sequence = { current: 0 }, response = deferred<string>();
    const commit = vi.fn(), failed = vi.fn(), settled = vi.fn();
    const pending = loadLatestResult(sequence, () => response.promise, commit, failed, settled);
    acceptDetailNavigation(sequence, () => true);
    response.resolve('private-track');
    expect(await pending).toBe(false);
    expect(commit).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled(); expect(settled).not.toHaveBeenCalled();
  });
});
