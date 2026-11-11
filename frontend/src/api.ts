import type { Session } from "./types";
let csrfToken = "";
let openAccess = false;
export function setSession(session: Session) {
  csrfToken = session.csrf_token || "";
  openAccess = session.authenticated && session.auth_enabled === false;
}
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD"].includes(method)) return request<T>(path, options);
  // A half-open connection must not pin the status/activity polling promise.
  // Include response-body reads and preserve the caller's cancellation.
  const controller = new AbortController();
  const parent = options.signal;
  const abort = () => controller.abort(parent?.reason);
  if (parent?.aborted) abort();
  else parent?.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 25_000);
  try {
    return await request<T>(path, { ...options, signal: controller.signal });
  } catch (error) {
    if (timedOut)
      throw new ApiError(
        "Сервер не ответил за 25 секунд. Проверьте соединение и обновите состояние.",
        408,
        "read_timeout",
      );
    throw error;
  } finally {
    clearTimeout(timer);
    parent?.removeEventListener("abort", abort);
  }
}
async function request<T>(path: string, options: RequestInit): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const mutation = options.method && !["GET", "HEAD"].includes(options.method.toUpperCase());
  if (mutation) headers.set("X-CSRF-Token", csrfToken);
  const send = (requestHeaders: Headers) =>
    fetch(`/api${path}`, {
      ...options,
      headers: requestHeaders,
      credentials: "same-origin",
      cache: "no-store",
    });
  let response = await send(headers);
  let payload = response.ok ? null : await response.json().catch(() => null);
  // A stale open-mode token is rejected before the action starts.
  if (
    mutation &&
    openAccess &&
    !path.startsWith("/auth/") &&
    response.status === 403 &&
    payload?.code === "csrf_invalid" &&
    (!options.body || typeof options.body === "string" || options.body instanceof FormData)
  ) {
    const fresh = await api<Session>("/auth/session", { signal: options.signal });
    if (openAccess && fresh.authenticated && fresh.auth_enabled === false && fresh.csrf_token) {
      setSession(fresh);
      const refreshedHeaders = new Headers(headers);
      refreshedHeaders.set("X-CSRF-Token", fresh.csrf_token);
      response = await send(refreshedHeaders);
      payload = response.ok ? null : await response.json().catch(() => null);
    }
  }
  if (!response.ok) {
    const detail = payload?.message || payload?.detail || payload?.error;
    const message =
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : response.status === 404 && payload === null
            ? mutation
              ? "Сервис недоступен. Перед повтором записи проверьте результат в разделе «Обработка»."
              : "Сервис недоступен. Обновите состояние после восстановления соединения."
            : `Сервер вернул ошибку ${response.status}`;
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      message,
      response.status,
      typeof payload?.code === "string" ? payload.code : undefined,
    );
  }
  return response.status === 204 ? (undefined as T) : await response.json();
}
export function messageFor(error: unknown): string {
  if (error instanceof ApiError && error.code === "owner_session_required")
    return "Для этого действия войдите в приложение в браузере.";
  if (error instanceof ApiError && ["field_locked", "field_lock_stale"].includes(error.code || ""))
    return error.message
      .replace(/решения владельца/g, "подтверждения")
      .replace(/владельцем/g, "вручную");
  if (
    error instanceof ApiError &&
    (error.status === 412 ||
      (error.status === 409 &&
        (!error.code ||
          ["revision_conflict", "proposal_conflict", "decision_conflict"].includes(error.code))))
  )
    return "Файл или предложение изменились. Обновите карточку перед сохранением. Ваш черновик пока остается на экране.";
  if (error instanceof TypeError)
    return "Не удалось связаться с сервером. Проверьте соединение и повторите действие.";
  return error instanceof Error ? error.message : "Не удалось выполнить действие.";
}

// randomUUID is restricted to secure contexts. Direct LAN demos still have getRandomValues.
export function requestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}
