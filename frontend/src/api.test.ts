import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, messageFor, requestId, setSession } from "./api";
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setSession({ authenticated: false });
});
describe("authenticated API client", () => {
  it("releases a hung read and allows the next status refresh", async () => {
    vi.useFakeTimers();
    let signal!: AbortSignal;
    const fetchMock = vi
      .fn()
      .mockImplementationOnce((_url, options) => {
        signal = options.signal;
        return new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      })
      .mockResolvedValueOnce(new Response('{"state":"succeeded"}'));
    vi.stubGlobal("fetch", fetchMock);
    const failed = expect(api("/summary")).rejects.toMatchObject({
      code: "read_timeout",
      status: 408,
    });
    await vi.advanceTimersByTimeAsync(25_000);
    await failed;
    expect(signal.aborted).toBe(true);
    await expect(api("/summary")).resolves.toEqual({ state: "succeeded" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });
  it("bounds a stalled response body even when the caller has a cancellation signal", async () => {
    vi.useFakeTimers();
    const parent = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_url, options) =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            new Promise((_resolve, reject) => {
              options.signal.addEventListener("abort", () => reject(options.signal.reason), {
                once: true,
              });
            }),
        }),
      ),
    );
    const failed = expect(
      api("/jobs/one/activity", { signal: parent.signal }),
    ).rejects.toMatchObject({ code: "read_timeout" });
    await vi.advanceTimersByTimeAsync(25_000);
    await failed;
    expect(parent.signal.aborted).toBe(false);
  });
  it("preserves caller cancellation and clears the read deadline", async () => {
    vi.useFakeTimers();
    const parent = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_url, options) =>
          new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => reject(options.signal.reason), {
              once: true,
            });
          }),
      ),
    );
    const error = new DOMException("caller stopped", "AbortError");
    const failed = expect(api("/summary", { signal: parent.signal })).rejects.toBe(error);
    parent.abort(error);
    await failed;
    expect(vi.getTimerCount()).toBe(0);
  });
  it("does not retry a write when the reverse proxy cannot route the request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("404 page not found", { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api("/jobs", { method: "POST" })).rejects.toThrow("Перед повтором записи");
    expect(fetchMock).toHaveBeenCalledOnce();
  });
  it("generates a random UUID on a direct LAN origin without randomUUID", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(255));
    vi.stubGlobal("crypto", { getRandomValues });
    expect(requestId()).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(getRandomValues.mock.calls[0][0]).toHaveLength(16);
  });
  it("sends session CSRF and revision guards with mutations", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ id: "proposal" }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    setSession({ authenticated: true, csrf_token: "test-csrf" });
    await api("/tracks/one/proposal", {
      method: "POST",
      headers: { "If-Match": '"revision"' },
      body: JSON.stringify({ tags: { title: "Название" } }),
    });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/tracks/one/proposal");
    expect(options.headers.get("X-CSRF-Token")).toBe("test-csrf");
    expect(options.headers.get("If-Match")).toBe('"revision"');
    expect(options.credentials).toBe("same-origin");
    expect(options.cache).toBe("no-store");
  });
  it("refreshes a rejected open-mode token once without losing write guards", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('{"code":"csrf_invalid"}', { status: 403 }))
      .mockResolvedValueOnce(
        new Response('{"authenticated":true,"auth_enabled":false,"csrf_token":"fresh"}'),
      )
      .mockResolvedValueOnce(new Response('{"id":"job"}', { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    setSession({ authenticated: true, auth_enabled: false, csrf_token: "old" });
    const body = '{"proposal_id":"one"}';
    await expect(
      api("/jobs", {
        method: "POST",
        headers: { "If-Match": '"revision"', "Idempotency-Key": "one-write" },
        body,
      }),
    ).resolves.toEqual({ id: "job" });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/jobs",
      "/api/auth/session",
      "/api/jobs",
    ]);
    expect(fetchMock.mock.calls[0][1].headers.get("X-CSRF-Token")).toBe("old");
    const retried = fetchMock.mock.calls[2][1];
    expect(retried.headers.get("X-CSRF-Token")).toBe("fresh");
    expect(retried.headers.get("If-Match")).toBe('"revision"');
    expect(retried.headers.get("Idempotency-Key")).toBe("one-write");
    expect(retried.body).toBe(body);
  });
  it("preserves rejection when login was re-enabled during the refresh", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('{"code":"csrf_invalid"}', { status: 403 }))
      .mockResolvedValueOnce(
        new Response('{"authenticated":true,"auth_enabled":true,"csrf_token":"owner"}'),
      );
    vi.stubGlobal("fetch", fetchMock);
    setSession({ authenticated: true, auth_enabled: false, csrf_token: "old" });
    await expect(api("/jobs", { method: "POST" })).rejects.toMatchObject({ code: "csrf_invalid" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it("never retries origin failures or requests in login mode", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('{"code":"origin_denied"}', { status: 403 }))
      .mockResolvedValueOnce(new Response('{"code":"csrf_invalid"}', { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    setSession({ authenticated: true, auth_enabled: false, csrf_token: "old" });
    await expect(api("/jobs", { method: "POST" })).rejects.toMatchObject({ code: "origin_denied" });
    setSession({ authenticated: true, auth_enabled: true, csrf_token: "old" });
    await expect(api("/jobs", { method: "POST" })).rejects.toMatchObject({ code: "csrf_invalid" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it("surfaces backend message and conflict guidance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "invalid_lrc",
            message: "Строка без времени",
            retryable: false,
          }),
          { status: 422 },
        ),
      ),
    );
    await expect(api("/jobs", { method: "POST" })).rejects.toThrow("Строка без времени");
    expect(
      messageFor(new ApiError("Нужна сессия владельца в браузере", 403, "owner_session_required")),
    ).toBe("Для этого действия войдите в приложение в браузере.");
    expect(messageFor(new ApiError("old", 412))).toContain("черновик");
    expect(
      messageFor(new ApiError("Источник не разрешает экспорт", 409, "source_policy_denied")),
    ).toBe("Источник не разрешает экспорт");
    expect(messageFor(new ApiError("Поле закреплено владельцем", 409, "field_locked"))).toBe(
      "Поле закреплено вручную",
    );
  });
  it("notifies the shell when an authenticated request expires", async () => {
    const target = new EventTarget();
    const listener = vi.fn();
    target.addEventListener("session-expired", listener);
    vi.stubGlobal("window", target);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));
    await expect(api("/tracks")).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce();
  });
});
