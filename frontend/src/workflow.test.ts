import { describe, expect, it } from "vitest";
import { nextCollectionAction, nextReviewId, publicationProgress } from "./workflow";
import type { Job } from "./types";

describe("collection workflow", () => {
  it("does not treat missing, empty, or inconsistent snapshots as complete", () => {
    expect(publicationProgress({}).percent).toBeNull();
    expect(publicationProgress({ audio_files: 0, published_current: 0 }).percent).toBeNull();
    expect(publicationProgress({ audio_files: 10, published_current: 11 }).percent).toBeNull();
    expect(
      publicationProgress({
        audio_files: 10,
        published_current: 2,
        with_cover: 10,
        with_lyrics: 10,
      }).percent,
    ).toBe(20);
  });
  it("keeps an active task visible and proposals separate from ready output", () => {
    const counts = { files: 120, published_current: 72, current_proposals: 18 };
    expect(nextCollectionAction(counts, [])).toBe("review");
    expect(nextCollectionAction(counts, [{ state: "waiting_provider" } as Job])).toBe("jobs");
    expect(nextCollectionAction({ files: 0 }, [])).toBe("guide");
  });
  it("does not loop or leave the captured review queue", () => {
    expect(nextReviewId(["a", "b", "c"], "b")).toBe("c");
    expect(nextReviewId(["a", "b"], "b")).toBeNull();
    expect(nextReviewId(["a", "b"], "missing")).toBeNull();
  });
});
