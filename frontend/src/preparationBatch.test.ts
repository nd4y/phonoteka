import { describe, expect, it } from "vitest";
import {
  preparationBatch,
  preparationSelection,
  selectedPreparationBatch,
  setPreparationSelected,
  type EvidencePreparation,
} from "./preparationBatch";

const item = (id: number, kind: "analyze" | "recognize", remaining?: number | null) => ({
  id,
  track_id: `track-${id}`,
  preparation: {
    token: "x",
    reason: "fixture",
    kind,
    fingerprint_requests_remaining: remaining,
  } as EvidencePreparation,
});

describe("preparation selection", () => {
  it("defaults to the original bounded cohort and its exact tokens", () => {
    const cached = item(1, "recognize", 0);
    const capacity = item(2, "recognize", 1);
    cached.preparation.token = "cached-revision";
    capacity.preparation.token = "capacity-revision";
    const plan = [cached, capacity];
    const chosen = selectedPreparationBatch(plan, preparationSelection(plan));
    expect(selectedPreparationBatch(plan, null)).toEqual(chosen);
    expect(chosen).toEqual([capacity, cached]);
    expect(chosen[0]).toBe(capacity);
    expect(chosen[1]).toBe(cached);
    expect(chosen.map((i) => [i.track_id, i.preparation.token])).toEqual([
      ["track-2", "capacity-revision"],
      ["track-1", "cached-revision"],
    ]);
  });

  it("does not replace an unchecked entry with a record outside the preview", () => {
    const plan = Array.from({ length: 51 }, (_, i) => item(i, "recognize", 0));
    const initial = preparationSelection(plan);
    const removed = setPreparationSelected(plan, initial, "track-0", false);
    const chosen = selectedPreparationBatch(plan, removed);
    expect(chosen).toHaveLength(49);
    expect(chosen).not.toContain(plan[50]);
    expect(preparationBatch(plan)).toHaveLength(50);
    expect(preparationBatch(plan)[0]).toBe(plan[0]);
    const restored = setPreparationSelected(plan, removed, "track-0", true);
    expect(selectedPreparationBatch(plan, restored)).toEqual(plan.slice(0, 50));
    expect(selectedPreparationBatch(plan, initial)).toEqual(plan.slice(0, 50));
  });

  it("keeps an empty choice empty and cannot select outside the fixed cohort", () => {
    const plan = [item(1, "analyze"), item(2, "recognize", 1)];
    const empty = setPreparationSelected(plan, preparationSelection(plan), "track-1", false);
    expect(selectedPreparationBatch(plan, empty)).toEqual([]);
    expect(
      selectedPreparationBatch(plan, setPreparationSelected(plan, empty, "track-2", true)),
    ).toEqual([]);
    expect(selectedPreparationBatch([], null)).toEqual([]);
  });

  it("binds choices to track IDs even when two records have the same token", () => {
    const plan = [item(1, "analyze"), item(2, "analyze")];
    const chosen = setPreparationSelected(plan, preparationSelection(plan), "track-1", false);
    expect(selectedPreparationBatch(plan, chosen)).toEqual([plan[1]]);
  });

  it("resets choices on a fresh plan even with identical IDs and tokens", () => {
    const plan = [item(1, "analyze"), item(2, "analyze")];
    const oldChoice = setPreparationSelected(plan, preparationSelection(plan), "track-1", false);
    const refreshed = [...plan];
    expect(selectedPreparationBatch(refreshed, oldChoice)).toEqual(refreshed);
    const freshChoice = setPreparationSelected(refreshed, oldChoice, "track-2", false);
    expect(selectedPreparationBatch(refreshed, freshChoice)).toEqual([refreshed[0]]);
  });

  it("never submits a replacement revision under a previously selected token", () => {
    const plan = [item(1, "analyze")];
    const oldChoice = preparationSelection(plan);
    plan[0] = { ...plan[0], preparation: { ...plan[0].preparation, token: "new-revision" } };
    expect(selectedPreparationBatch(plan, oldChoice)).toEqual([]);
    const refreshed = [...plan];
    expect(selectedPreparationBatch(refreshed, oldChoice)[0].preparation.token).toBe(
      "new-revision",
    );
  });
});
describe("preparation priorities", () => {
  it("searches metadata before consuming recognition or checking exhausted caches", () => {
    const values = [item(1, "recognize", 0), item(2, "recognize", 2), item(3, "analyze")];
    expect(preparationBatch(values).map((i) => i.id)).toEqual([3]);
  });
  it("fills recognition slots with cache checks after available or unknown capacity", () => {
    const values = [item(1, "recognize", 0), item(2, "recognize", 1), item(3, "recognize", null)];
    expect(preparationBatch(values).map((i) => i.id)).toEqual([2, 3, 1]);
    expect(preparationBatch([values[0]]).map((i) => i.id)).toEqual([1]);
  });
  it("does not strand 48 recoveries when one fresh recognition becomes available", () => {
    const recovered = Array.from({ length: 48 }, (_, i) => item(i, "recognize", 0));
    const capacity = item(99, "recognize", 1);
    expect(preparationBatch([...recovered, capacity]).map((i) => i.id)).toEqual([
      99,
      ...recovered.map((i) => i.id),
    ]);
  });
  it("keeps the 50-record bound and capacity priority when both groups are large", () => {
    const cached = Array.from({ length: 60 }, (_, i) => item(i, "recognize", 0));
    const capacity = Array.from({ length: 55 }, (_, i) => item(100 + i, "recognize", 1));
    expect(preparationBatch([...cached, ...capacity]).map((i) => i.id)).toEqual(
      capacity.slice(0, 50).map((i) => i.id),
    );
  });
  it("retains the exact frozen entries and tokens without sorting the input in place", () => {
    const cached = item(1, "recognize", 0);
    cached.preparation.token = "frozen-cache-token";
    const capacity = item(2, "recognize", 1);
    const values = [cached, capacity];
    const snapshot = JSON.stringify(values);
    const chosen = preparationBatch(values);
    expect(chosen[0]).toBe(capacity);
    expect(chosen[1]).toBe(cached);
    expect(chosen[1].preparation.token).toBe("frozen-cache-token");
    expect(JSON.stringify(values)).toBe(snapshot);
  });
  it("bounds the frozen preview and preserves stable order without mutation", () => {
    const values = Array.from({ length: 70 }, (_, i) => item(i, "analyze"));
    expect(preparationBatch(values).map((i) => i.id)).toEqual(
      Array.from({ length: 50 }, (_, i) => i),
    );
    expect(values).toHaveLength(70);
    expect(preparationBatch([{ id: 1, preparation: null }])).toEqual([]);
  });
});
