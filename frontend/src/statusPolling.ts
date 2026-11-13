const TICK_MS = 4000;

export function startStatusPolling(
  refresh: () => Promise<void>,
  visible: () => boolean,
  getIntervalMs: () => number = () => TICK_MS,
) {
  let pending = false;
  let stopped = false;
  let lastDispatch = Date.now();
  let wasVisible = visible();
  let wake = false;

  const intervalMs = () => {
    const value = getIntervalMs();
    return Number.isFinite(value) ? Math.max(TICK_MS, value) : TICK_MS;
  };

  const done = () => {
    pending = false;
  };

  const poll = () => {
    if (stopped || pending) return;
    pending = true;
    lastDispatch = Date.now();
    void refresh().then(done, done);
  };

  poll();

  const timer = setInterval(() => {
    const currentlyVisible = visible();
    if (currentlyVisible && !wasVisible) wake = true;
    wasVisible = currentlyVisible;
    if (!currentlyVisible || pending) return;
    if (wake || Date.now() - lastDispatch >= intervalMs()) {
      wake = false;
      poll();
    }
  }, TICK_MS);

  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
