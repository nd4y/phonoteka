type Visibility = Pick<Document, "hidden" | "addEventListener" | "removeEventListener">;

export function startPreparationPolling(
  refresh: (signal: AbortSignal) => Promise<void>,
  active: boolean,
  visibility: Visibility = document,
) {
  let disposed = false;
  let pending = false;
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout>;
  async function poll() {
    if (disposed || pending) return;
    if (visibility.hidden) {
      timer = setTimeout(() => void poll(), 5000);
      return;
    }
    pending = true;
    controller = new AbortController();
    let delay = active ? 5000 : 30000;
    try {
      await refresh(controller.signal);
    } catch {
      delay = active ? 10000 : 30000;
    } finally {
      pending = false;
      if (!disposed) timer = setTimeout(() => void poll(), delay);
    }
  }
  function wake() {
    if (!visibility.hidden) {
      clearTimeout(timer);
      void poll();
    }
  }
  visibility.addEventListener("visibilitychange", wake);
  void poll();
  return () => {
    disposed = true;
    clearTimeout(timer);
    controller?.abort();
    visibility.removeEventListener("visibilitychange", wake);
  };
}
