import { describe, expect, it } from "vitest";
import { reviewNextAction } from "./reviewNextAction";

describe("review next step", () => {
  it("saves an edited draft, including untimed lyrics, before offering any write", () => {
    expect(reviewNextAction({ changed: true, savedProposal: true, invalidLyrics: true })).toBe(
      "save",
    );
  });
  it("takes saved untimed or invalid lyrics to the editor rather than a blocked write", () => {
    expect(reviewNextAction({ changed: false, savedProposal: true, invalidLyrics: true })).toBe(
      "lyrics",
    );
  });
  it("offers a write only for a saved, unchanged valid proposal", () => {
    expect(reviewNextAction({ changed: false, savedProposal: true, invalidLyrics: false })).toBe(
      "write",
    );
    expect(reviewNextAction({ changed: true, savedProposal: true, invalidLyrics: false })).toBe(
      "save",
    );
    expect(reviewNextAction({ changed: false, savedProposal: false, invalidLyrics: false })).toBe(
      "find",
    );
  });
});
