import { api, messageFor } from './api';
import { mergeActivityEvents } from './jobs';
import type { ActivityEvent, JobActivity } from './jobs';

export type ActivityFeedState = {
  jobId: string; activity: JobActivity | null; events: ActivityEvent[];
  connection: 'connecting' | 'live' | 'reconnecting' | 'offline'; error: string;
};
const isOnline = () => typeof navigator === 'undefined' || navigator.onLine !== false;
export const emptyActivityFeed = (jobId: string): ActivityFeedState => ({
  jobId, activity: null, events: [], connection: isOnline() ? 'connecting' : 'offline', error: '',
});

// Each subscription owns its requests, stream and timers. Closed EventSource
// instances cannot reconnect themselves; ordinary CONNECTING ones still can.
export function subscribeJobActivity(jobId: string, publish: (state: ActivityFeedState) => void, refreshJobs: () => void) {
  let state = emptyActivityFeed(jobId);
  let stopped = false;
  let source: EventSource | undefined;
  let pending: Promise<void> | undefined;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  const controller = new AbortController();
  function update(patch: Partial<ActivityFeedState>) {
    if (!stopped) { state = { ...state, ...patch }; publish(state); }
  }
  function connect(cursor: number) {
    if (stopped || !isOnline()) return;
    if (source && source.readyState !== EventSource.CLOSED) {
      if (source.readyState === EventSource.OPEN) update({ connection: 'live' });
      return;
    }
    source?.close();
    const stream = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events?after=${cursor}`);
    source = stream;
    const current = () => !stopped && source === stream;
    stream.onopen = () => { if (current()) update({ connection: 'live' }); };
    stream.onerror = () => {
      if (!current()) return;
      update({ connection: isOnline() ? 'reconnecting' : 'offline' });
      if (stream.readyState === EventSource.CLOSED) { stream.close(); source = undefined; }
    };
    stream.addEventListener('job_activity', event => {
      if (!current()) return;
      try {
        const incoming = JSON.parse((event as MessageEvent<string>).data) as ActivityEvent;
        update({ events: mergeActivityEvents(state.events, [incoming]) });
        if (!refreshTimer) refreshTimer = setTimeout(() => {
          refreshTimer = undefined;
          if (!stopped) { void load(); refreshJobs(); }
        }, 350);
      } catch { /* A periodic snapshot recovers a malformed/interrupted event. */ }
    });
    stream.addEventListener('resync_required', () => {
      if (current()) { update({ events: [] }); void load(); }
    });
  }
  function load(): Promise<void> {
    if (stopped) return Promise.resolve();
    if (pending) return pending;
    pending = (async () => {
      try {
        const snapshot = await api<JobActivity>(`/jobs/${encodeURIComponent(jobId)}/activity?limit=200`, { signal: controller.signal });
        if (stopped) return;
        update({ activity: snapshot, error: '', events: snapshot.resync_required ? snapshot.events : mergeActivityEvents(state.events, snapshot.events) });
        connect(snapshot.cursor);
      } catch (err) {
        update({ error: messageFor(err), connection: isOnline() ? 'reconnecting' : 'offline' });
      } finally { pending = undefined; }
    })();
    return pending;
  }
  const offline = () => update({ connection: 'offline' });
  const online = () => { update({ connection: 'reconnecting' }); void load(); };
  window.addEventListener('offline', offline);
  window.addEventListener('online', online);
  const poll = setInterval(() => { void load(); }, 6000);
  publish(state); void load();
  return {
    refresh: load,
    stop() {
      stopped = true; controller.abort(); source?.close();
      clearInterval(poll); clearTimeout(refreshTimer);
      window.removeEventListener('offline', offline); window.removeEventListener('online', online);
    },
  };
}
