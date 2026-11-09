export function startStatusPolling(refresh: () => Promise<void>, visible: () => boolean) {
  let pending = false;
  const done = () => {
    pending = false;
  };
  const poll = () => {
    if (pending) return;
    pending = true;
    void refresh().then(done, done);
  };
  poll();
  const timer = setInterval(() => {
    if (visible()) poll();
  }, 4000);
  return () => clearInterval(timer);
}
