import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribeJobActivity } from './jobActivityFeed';
import type { ActivityFeedState } from './jobActivityFeed';

class Stream extends EventTarget {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  static instances: Stream[] = [];
  readyState = Stream.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) { super(); Stream.instances.push(this); }
  close() { this.readyState = Stream.CLOSED; }
  opened() { this.readyState = Stream.OPEN; this.onopen?.(); }
  failed(permanent = true) { this.readyState = permanent ? Stream.CLOSED : Stream.CONNECTING; this.onerror?.(); }
  message(id: number) { this.dispatchEvent(new MessageEvent('job_activity', { data: JSON.stringify({ id, message: 'Этап завершен' }) })); }
}
const snapshot = (id = 'one', cursor = 1) => new Response(JSON.stringify({
  job: { id, state: 'running' }, cursor, events: [{ id: cursor, message: `Снимок ${id}` }],
  active_items: [], active_items_truncated: false, resync_required: false, has_more: false,
}));
const feeds: ReturnType<typeof subscribeJobActivity>[] = [];
function subscribe(id = 'one') {
  const publish = vi.fn<(state: ActivityFeedState) => void>(); const refresh = vi.fn();
  const feed = subscribeJobActivity(id, publish, refresh); feeds.push(feed);
  return { ...feed, publish, refreshJobs: refresh, latest: () => publish.mock.calls.at(-1)![0] };
}
beforeEach(() => {
  vi.useFakeTimers(); Stream.instances = [];
  vi.stubGlobal('window', new EventTarget()); vi.stubGlobal('navigator', { onLine: true });
  vi.stubGlobal('EventSource', Stream);
});
afterEach(() => { for (const feed of feeds.splice(0)) feed.stop(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('live job subscription recovery', () => {
  it('recreates a permanently closed stream from a fresh snapshot cursor', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(snapshot('one', 25));
    vi.stubGlobal('fetch', fetch);
    const feed = subscribe(); await feed.refresh();
    const old = Stream.instances[0]; old.opened(); old.failed();
    expect(feed.latest().connection).toBe('reconnecting');
    await vi.advanceTimersByTimeAsync(6000);
    expect(Stream.instances).toHaveLength(2);
    expect(Stream.instances[1].url).toContain('after=25');
    Stream.instances[1].opened(); old.message(99);
    expect(feed.latest().connection).toBe('live');
    expect(feed.latest().events.map(event => event.id)).toEqual([1, 25]);
  });
  it('keeps a stream that is still performing native reconnection', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(snapshot())));
    const feed = subscribe(); await feed.refresh();
    Stream.instances[0].failed(false); await vi.advanceTimersByTimeAsync(12000);
    expect(Stream.instances).toHaveLength(1);
    Stream.instances[0].opened(); expect(feed.latest().connection).toBe('live');
  });
  it('recovers after the initial HTTP failure and does not need a page reload', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(snapshot()));
    const feed = subscribe(); await feed.refresh();
    expect(feed.latest().error).toContain('соединение');
    await vi.advanceTimersByTimeAsync(6000);
    expect(Stream.instances).toHaveLength(1); expect(feed.latest().error).toBe('');
  });
  it('discards old in-flight data when switching jobs and aborts its request', async () => {
    let resolve!: (response: Response) => void;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(done => { resolve = done; }))
      .mockResolvedValueOnce(snapshot('two', 20));
    vi.stubGlobal('fetch', fetch);
    const first = subscribe(); const pending = first.refresh(); first.stop();
    const second = subscribe('two'); await second.refresh();
    resolve(snapshot()); await pending;
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    expect(first.publish).toHaveBeenCalledTimes(1);
    expect(second.latest().jobId).toBe('two'); expect(second.latest().events.map(event => event.id)).toEqual([20]);
    expect(Stream.instances).toHaveLength(1);
  });
  it('stops delayed refreshes, stream callbacks and timers on unmount', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(snapshot()); vi.stubGlobal('fetch', fetch);
    const feed = subscribe(); await feed.refresh(); const stream = Stream.instances[0];
    stream.message(2); feed.stop(); const count = feed.publish.mock.calls.length;
    stream.opened(); stream.message(3); window.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(12000);
    expect(fetch).toHaveBeenCalledTimes(1); expect(feed.refreshJobs).not.toHaveBeenCalled();
    expect(feed.publish).toHaveBeenCalledTimes(count);
  });
  it('uses the normal session-expiry path when reconnecting receives 401', async () => {
    const expired = vi.fn(); window.addEventListener('session-expired', expired);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response('{}', { status: 401 })));
    const feed = subscribe(); await feed.refresh(); Stream.instances[0].failed();
    await vi.advanceTimersByTimeAsync(6000);
    expect(expired).toHaveBeenCalledOnce(); expect(Stream.instances).toHaveLength(1);
  });
});
