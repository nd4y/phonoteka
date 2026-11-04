import type { Session } from "./types";
let csrfToken = "";
export function setSession(session: Session) {
  csrfToken = session.csrf_token || "";
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
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method))
    headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.message || payload?.detail || payload?.error;
    const message =
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : `Сервер вернул ошибку ${response.status}`;
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      message,
      response.status,
      typeof payload?.code === "string" ? payload.code : undefined,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
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
