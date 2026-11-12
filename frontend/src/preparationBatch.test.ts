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
  it("retains cache-only checks after available recognition and never mixes groups", () => {
    const values = [item(1, "recognize", 0), item(2, "recognize", 1), item(3, "recognize", null)];
    expect(preparationBatch(values).map((i) => i.id)).toEqual([2, 3]);
    expect(preparationBatch([values[0]]).map((i) => i.id)).toEqual([1]);
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
