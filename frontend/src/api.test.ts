import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, messageFor, requestId, setSession } from "./api";
afterEach(() => {
  vi.unstubAllGlobals();
  setSession({ authenticated: false });
});
describe("authenticated API client", () => {
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
