import { afterEach, describe, expect, it, vi } from "vitest";
import { AppHistory } from "./appHistory";
import {
  clearView,
  emptyView,
  parseView,
  readSourcesOpen,
  readView,
  saveSourcesOpen,
  saveView,
  sourcesOpenKey,
  viewKey,
} from "./viewState";
import type { ViewState } from "./viewState";

function storageFixture() {
  const values = new Map<string, string>();
  const store = {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      values.set(key, value);
    }),
    removeItem: vi.fn((key: string) => {
      values.delete(key);
    }),
  };
  vi.stubGlobal("sessionStorage", store);
  return { values, store };
}

afterEach(() => vi.unstubAllGlobals());

describe("reload view state", () => {
  it("restores a validated wizard step without persisting server data", () => {
    expect(parseView('{"page":"guide","guideStep":"metadata"}')).toMatchObject({
      page: "guide",
      guideStep: "metadata",
    });
    expect(parseView('{"page":"help","guideStep":"delete"}').guideStep).toBeUndefined();
  });
  it.each([
    "audit_errors",
    "audit_warnings",
    "audit_unchecked",
    "audit_passed",
    "review_prepare",
    "review_choose",
    "review_listen",
    "review_file",
  ])("restores the %s catalog context without losing editor, query or list position", (filter) => {
    const state = {
      page: "review",
      query: "WEBM %_",
      filter,
      pageNumber: 4,
      selectedId: "fixture-002",
      scroll: 813,
      queueScroll: 120,
    };
    expect(parseView(JSON.stringify(state))).toEqual(state);
  });

  it("restores an editor and a filtered catalog beyond the first page", () => {
    const state = {
      page: "lyrics",
      query: "Би-2 %_",
      filter: "missing_lyrics",
      pageNumber: 9,
      selectedId: "track-42",
      scroll: 1217,
      queueScroll: 150,
    };
    expect(parseView(JSON.stringify(state))).toEqual(state);
  });
  it("rejects malformed storage and clamps browser supplied values", () => {
    expect(parseView("null")).toEqual(emptyView);
    expect(parseView("broken")).toEqual(emptyView);
    expect(
      parseView(
        JSON.stringify({
          page: "secret",
          filter: "invalid",
          query: 42,
          selectedId: {},
          pageNumber: -2,
          scroll: -12,
          queueScroll: "a",
        }),
      ),
    ).toEqual(emptyView);
    expect(parseView('{"scroll":1e99,"pageNumber":999999}')).toMatchObject({
      scroll: 1e7,
      pageNumber: 100000,
    });
  });
});

describe("editor sources session state", () => {
  const editor: ViewState = {
    ...emptyView,
    page: "review",
    query: "WEBM",
    filter: "audit_errors",
    selectedId: "fixture-002",
    scroll: 813,
    queueScroll: 120,
  };

  it("restores Sources alongside the existing editor and list context on reload", () => {
    const { values } = storageFixture();
    saveView(editor);
    saveSourcesOpen(true);
    expect(readView()).toEqual(editor);
    expect(readSourcesOpen()).toBe(true);
    expect(values.get(sourcesOpenKey)).toBe("true");
    expect([...values.keys()].sort()).toEqual([sourcesOpenKey, viewKey].sort());
    saveSourcesOpen(false);
    expect(readSourcesOpen()).toBe(false);
    expect(values.get(sourcesOpenKey)).toBe("false");
    expect(readView()).toEqual(editor);
  });

  it("persists a Back dismissal of Sources without navigating away from the record", () => {
    const { store } = storageFixture();
    saveView(editor);
    saveSourcesOpen(true);
    let sourcesOpen = readSourcesOpen();
    const apply = vi.fn();
    const go = vi.fn(() => true);
    const history = new AppHistory({
      initial: readView(),
      token: "sources-session-fixture",
      store,
      port: { replace: () => true, push: () => true, go },
      capture: () => editor,
      apply,
      approve: () => true,
      changed: vi.fn(),
      dismiss: () => {
        if (!sourcesOpen) return false;
        sourcesOpen = false;
        saveSourcesOpen(sourcesOpen);
        return true;
      },
    });
    history.back();
    expect(readSourcesOpen()).toBe(false);
    expect(readView()).toEqual(editor);
    expect(history.current).toEqual(editor);
    expect(go).not.toHaveBeenCalled();
    expect(apply).not.toHaveBeenCalled();
  });

  it.each([null, "false", "1", '"true"', '{"open":true}', "null", "broken"])(
    "defaults missing or non-true session storage %s to closed",
    (raw) => {
      const { values } = storageFixture();
      if (raw !== null) values.set(sourcesOpenKey, raw);
      expect(readSourcesOpen()).toBe(false);
    },
  );

  it("accepts only a boolean and never stores an object or track data in the Sources key", () => {
    const { values } = storageFixture();
    saveSourcesOpen(true);
    saveSourcesOpen({ selectedId: "fixture-002" } as unknown as boolean);
    expect(values.get(sourcesOpenKey)).toBe("true");
  });

  it("keeps navigation and logout available when session storage is denied", () => {
    const denied = vi.fn(() => {
      throw new Error("storage denied");
    });
    vi.stubGlobal("sessionStorage", { getItem: denied, setItem: denied, removeItem: denied });
    expect(readSourcesOpen()).toBe(false);
    expect(() => saveSourcesOpen(true)).not.toThrow();
    expect(() => clearView()).not.toThrow();
    expect(denied).toHaveBeenCalledWith(viewKey);
    expect(denied).toHaveBeenCalledWith(sourcesOpenKey);
  });

  it("clears both known session keys on logout while preserving unrelated storage", () => {
    const { values, store } = storageFixture();
    saveView(editor);
    saveSourcesOpen(true);
    values.set("unrelated-session-key", "keep");
    clearView();
    expect(readView()).toEqual(emptyView);
    expect(readSourcesOpen()).toBe(false);
    expect([...values.entries()]).toEqual([["unrelated-session-key", "keep"]]);
    expect(store.removeItem.mock.calls).toEqual([[viewKey], [sourcesOpenKey]]);
  });

  it("still removes Sources when removing the saved view is denied", () => {
    const { values, store } = storageFixture();
    saveView(editor);
    saveSourcesOpen(true);
    store.removeItem.mockImplementation((key) => {
      if (key === viewKey) throw new Error("view storage denied");
      values.delete(key);
    });
    expect(() => clearView()).not.toThrow();
    expect(values.has(sourcesOpenKey)).toBe(false);
    expect(values.has(viewKey)).toBe(true);
  });
});
