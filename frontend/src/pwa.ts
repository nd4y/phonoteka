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
function publish(next: Partial<PwaState>) {
  state = { ...state, ...next };
  subscribers.forEach(callback => callback());
}
export function usePwa() {
  return useSyncExternalStore(callback => { subscribers.add(callback); return () => { subscribers.delete(callback); }; }, () => state);
}
export function dismissPwaError() { publish({ error: null }); }

async function connectWorker() {
  registration = await navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' });
  const updateState = () => publish({ updateReady: Boolean(registration?.waiting) });
  updateState();
  registration.addEventListener('updatefound', () => {
    const worker = registration?.installing;
    worker?.addEventListener('statechange', () => {
      if (worker.state === 'installed') updateState();
      if (worker.state === 'redundant' && !state.offlineReady) publish({ error: 'Не удалось сохранить интерфейс для запуска без сети. Повторите проверку обновлений.' });
    });
  });
  void navigator.serviceWorker.ready.then(() => publish({ offlineReady: true }));
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
  } catch { publish({ error: 'Не удалось проверить обновление. Сервер должен быть доступен.' }); }
}

// Called only after the owner's explicit click and optional draft confirmation.
// Other tabs may receive a new controller, but never reload automatically.
export async function activatePwaUpdate(onBeforeReload?: () => void) {
  const waiting = registration?.waiting;
  if (!waiting || state.updating) return;
  publish({ updating: true, error: null });
  try {
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        navigator.serviceWorker.removeEventListener('controllerchange', changed);
        reject(new Error('activation timeout'));
      }, 15000);
      const changed = () => {
        window.clearTimeout(timeout);
        navigator.serviceWorker.removeEventListener('controllerchange', changed);
        resolve();
      };
      navigator.serviceWorker.addEventListener('controllerchange', changed);
      waiting.postMessage({ type: 'PHONOTEKA_ACTIVATE' });
    });
    onBeforeReload?.();
    window.location.reload();
  } catch { publish({ updating: false, error: 'Обновление пока не применилось. Ваши данные остаются на экране. Повторите попытку.' }); }
}
