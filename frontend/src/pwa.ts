import { useSyncExternalStore } from 'react';

interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}
export interface PwaState {
  supported: boolean;
  installed: boolean;
  installable: boolean;
  offline: boolean;
  offlineReady: boolean;
  updateReady: boolean;
  updating: boolean;
  error: string | null;
}
const standalone = () => window.matchMedia('(display-mode: standalone)').matches
  || Boolean((navigator as Navigator & { standalone?: boolean }).standalone);
let state: PwaState = {
  supported: window.isSecureContext && 'serviceWorker' in navigator,
  installed: standalone(), installable: false, offline: !navigator.onLine,
  offlineReady: false, updateReady: false, updating: false, error: null,
};
const subscribers = new Set<() => void>();
let registration: ServiceWorkerRegistration | null = null;
let installPrompt: InstallPrompt | null = null;
let started = false;
let watchingController = false;
const watchedWorkers = new WeakSet<ServiceWorker>();
const watchedRegistrations = new WeakSet<ServiceWorkerRegistration>();
function publish(next: Partial<PwaState>) {
  state = { ...state, ...next };
  subscribers.forEach(callback => callback());
}
export function usePwa() {
  return useSyncExternalStore(callback => { subscribers.add(callback); return () => { subscribers.delete(callback); }; }, () => state);
}
export function dismissPwaError() { publish({ error: null }); }

function syncWorkerState() {
  const current = registration;
  if (!current) return;
  for (const worker of [current.installing, current.waiting, current.active]) {
    if (!worker || watchedWorkers.has(worker)) continue;
    watchedWorkers.add(worker);
    worker.addEventListener('statechange', () => {
      syncWorkerState();
      // Registration slots can change in the same lifecycle turn.
      queueMicrotask(syncWorkerState);
      if (worker.state === 'redundant' && !state.offlineReady && !current.active) {
        publish({ error: 'Не удалось сохранить интерфейс для запуска без сети. Повторите проверку обновлений.' });
      }
    });
  }
  const waiting = current.waiting;
  // The first install briefly passes through "waiting" before activation.
  // An update requires a distinct existing active worker/controller.
  const updateReady = waiting?.state === 'installed' && waiting !== current.active
    && Boolean(current.active || navigator.serviceWorker.controller);
  publish({
    updateReady,
    offlineReady: state.offlineReady || current.active?.state === 'activated'
      || navigator.serviceWorker.controller?.state === 'activated',
  });
}

async function connectWorker() {
  registration = await navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' });
  if (!watchedRegistrations.has(registration)) {
    watchedRegistrations.add(registration);
    registration.addEventListener('updatefound', syncWorkerState);
  }
  if (!watchingController) {
    watchingController = true;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      // A different tab can accept an update. Synchronize this tab's banner,
      // but preserve its playback and drafts by never reloading here.
      syncWorkerState();
      queueMicrotask(syncWorkerState);
    });
  }
  syncWorkerState();
  void navigator.serviceWorker.ready.then(() => {
    syncWorkerState();
    publish({ offlineReady: true });
  });
}

export function registerPwa() {
  if (started) return;
  started = true;
  window.addEventListener('online', () => { publish({ offline: false }); void registration?.update().catch(() => undefined); });
  window.addEventListener('offline', () => publish({ offline: true }));
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event as InstallPrompt;
    publish({ installable: true });
  });
  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    publish({ installed: true, installable: false });
  });
  const mode = window.matchMedia('(display-mode: standalone)');
  mode.addEventListener('change', () => publish({ installed: standalone() }));
  // Vite's dev server has no generated worker. Preview and Docker builds do.
  if (!state.supported || !import.meta.env.PROD) return;
  const start = async () => {
    try {
      await connectWorker();
    } catch {
      publish({ error: 'Не удалось включить режим приложения. Проверьте соединение и повторите проверку обновлений.' });
    }
  };
  if (document.readyState === 'complete') void start();
  else window.addEventListener('load', () => { void start(); }, { once: true });
}

export async function promptPwaInstall(): Promise<boolean> {
  if (!installPrompt) return false;
  const prompt = installPrompt;
  installPrompt = null;
  publish({ installable: false });
  try {
    await prompt.prompt();
    await prompt.userChoice;
    return true;
  } catch {
    publish({ error: 'Установка не открылась. Используйте команду «Установить приложение» в меню браузера.' });
    return false;
  }
}

export async function checkPwaUpdate() {
  publish({ error: null });
  try {
    if (registration) await registration.update();
    else if (state.supported && import.meta.env.PROD) await connectWorker();
    syncWorkerState();
  } catch { publish({ error: 'Не удалось проверить обновление. Сервер должен быть доступен.' }); }
}

// Called only after the owner's explicit click and optional draft confirmation.
// Other tabs may receive a new controller, but never reload automatically.
export interface PwaUpdateCallbacks {
  onBeforeActivate?: () => void;
  onBeforeReload?: () => void;
  onActivationFailed?: () => void;
}
export async function activatePwaUpdate({ onBeforeActivate, onBeforeReload, onActivationFailed }: PwaUpdateCallbacks = {}) {
  if (state.updating) return false;
  try {
    // Freeze navigation before any worker/store notification can change layout.
    // Playback is deliberately sampled later, immediately before the reload.
    onBeforeActivate?.();
    syncWorkerState();
    const waiting = registration?.waiting;
    if (!waiting || !state.updateReady) {
      publish({ updateReady: false, error: 'Готового обновления больше нет. Проверьте обновления ещё раз.' });
      onActivationFailed?.();
      return false;
    }
    publish({ updating: true, error: null });
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        navigator.serviceWorker.removeEventListener('controllerchange', changed);
        reject(new Error('activation timeout'));
      }, 15000);
      const changed = () => {
        if (navigator.serviceWorker.controller !== waiting) return;
        window.clearTimeout(timeout);
        navigator.serviceWorker.removeEventListener('controllerchange', changed);
        resolve();
      };
      navigator.serviceWorker.addEventListener('controllerchange', changed);
      waiting.postMessage({ type: 'PHONOTEKA_ACTIVATE' });
    });
    onBeforeReload?.();
    window.location.reload();
    return true;
  } catch {
    syncWorkerState();
    publish({ updating: false, error: 'Обновление пока не применилось. Ваши данные остаются на экране. Повторите попытку.' });
    onActivationFailed?.();
    return false;
  }
}
