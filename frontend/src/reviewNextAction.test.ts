import { describe, expect, it } from "vitest";
import { reviewNextAction } from "./reviewNextAction";

describe("review next step", () => {
  it("prepares drafts and resolves blocking recording decisions before any write", () => {
    const base = { changed: false, savedProposal: true, invalidLyrics: false };
    expect(reviewNextAction({ ...base, preparation: true })).toBe("prepare");
    expect(reviewNextAction({ ...base, possibleMix: true })).toBe("recording");
    expect(reviewNextAction({ ...base, fileProblem: true })).toBe("file");
    expect(reviewNextAction({ ...base, changed: true, preparation: true })).toBe("save");
  });
  it("offers a separate readable copy before writing a file with a decoder failure", () => {
    expect(
      reviewNextAction({
        changed: false,
        savedProposal: true,
        invalidLyrics: false,
        canRecover: true,
      }),
    ).toBe("recover");
    expect(
      reviewNextAction({
        changed: true,
        savedProposal: true,
        invalidLyrics: false,
        canRecover: true,
      }),
    ).toBe("save");
  });
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
  it("offers to write saved tags or artwork while the existing lyrics still need work", () => {
    const draft = {
      changed: false,
      savedProposal: true,
      savedMetadataOnly: true,
      invalidLyrics: true,
      preparation: true,
    };
    expect(reviewNextAction(draft)).toBe("write");
    expect(reviewNextAction({ ...draft, possibleMix: true })).toBe("recording");
    expect(reviewNextAction({ ...draft, fileProblem: true })).toBe("file");
    expect(reviewNextAction({ ...draft, changed: true })).toBe("save");
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
