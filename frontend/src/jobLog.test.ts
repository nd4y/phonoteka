import { describe, expect, it } from "vitest";
import { filterJobEvents } from "./jobLog";
import type { ActivityEvent } from "./jobs";
const events: ActivityEvent[] = [
  {
    id: 1,
    kind: "stage",
    created_at: "2026-11-02T00:00:00Z",
    message: "Обложка сохранена",
    filename: "Песня.flac",
    state: "succeeded",
  },
  {
    id: 2,
    kind: "stage",
    created_at: "2026-11-02T00:00:01Z",
    message: "Нужно подтверждение",
    code: "authorization_required",
    state: "waiting_authorization",
  },
  {
    id: 3,
    kind: "error",
    created_at: "2026-11-02T00:00:02Z",
    message: "Сервис недоступен",
    provider: "acoustid",
  },
];
describe("loaded event search", () => {
  it("searches localized messages and filenames without changing chronology or input", () => {
    expect(filterJobEvents(events, "  ПЕСНЯ  ", false).map((e) => e.id)).toEqual([1]);
    expect(filterJobEvents(events, "ОБЛОЖКА", false).map((e) => e.id)).toEqual([1]);
    expect(events.map((e) => e.id)).toEqual([1, 2, 3]);
  });
  it("includes authorization needs in problems and composes filters with code/provider search", () => {
    expect(filterJobEvents(events, "", true).map((e) => e.id)).toEqual([2, 3]);
    expect(filterJobEvents(events, "authorization", true).map((e) => e.id)).toEqual([2]);
    expect(filterJobEvents(events, "ACOUSTID", true).map((e) => e.id)).toEqual([3]);
    expect(filterJobEvents(events, "Песня", true)).toEqual([]);
  });
});
