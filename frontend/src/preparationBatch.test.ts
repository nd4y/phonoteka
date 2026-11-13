import { describe, expect, it } from "vitest";
import { preparationBatch, type EvidencePreparation } from "./preparationBatch";

const item = (id: number, kind: "analyze" | "recognize", remaining?: number | null) => ({
  id,
  preparation: {
    token: "x",
    reason: "fixture",
    kind,
    fingerprint_requests_remaining: remaining,
  } as EvidencePreparation,
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
