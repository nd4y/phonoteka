import { describe, expect, it, vi } from "vitest";
import {
  awaitReviewWrite,
  nextCollectionAction,
  nextReviewId,
  publicationProgress,
} from "./workflow";
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

const queued = { id: "write-1", state: "queued", completed: 0, failed: 0 } as Job;
const done = { ...queued, state: "succeeded", completed: 1 };
describe("review write boundary", () => {
  it("waits for a completed file, not admission", async () => {
    const read = vi
      .fn()
      .mockResolvedValueOnce({ ...queued, state: "running" })
      .mockResolvedValueOnce(done);
    expect(
      await awaitReviewWrite(
        queued,
        read,
        () => true,
        async () => {},
      ),
    ).toBe("written");
    expect(read).toHaveBeenCalledTimes(2);
    expect(read).toHaveBeenCalledWith("write-1");
  });
  it("keeps errors, cancellations and zero-item successes on the current record", async () => {
    for (const job of [
      { ...queued, state: "failed" },
      { ...queued, state: "cancelled" },
      { ...queued, state: "succeeded" },
    ]) {
      expect(await awaitReviewWrite(job, vi.fn(), () => true)).toBe("failed");
    }
  });
  it("keeps authorization and slow writes pending without resubmitting", async () => {
    const read = vi.fn().mockResolvedValue(queued);
    expect(
      await awaitReviewWrite({ ...queued, stage: "waiting_authorization" }, read, () => true),
    ).toBe("pending");
    expect(read).not.toHaveBeenCalled();
    expect(
      await awaitReviewWrite(
        queued,
        read,
        () => true,
        async () => {},
        2,
      ),
    ).toBe("pending");
    expect(read).toHaveBeenCalledTimes(2);
  });
  it("abandons navigation when the user leaves and propagates unknown transport failures", async () => {
    const read = vi.fn().mockResolvedValue(done);
    let current = true;
    expect(
      await awaitReviewWrite(
        queued,
        read,
        () => current,
        async () => {
          current = false;
        },
      ),
    ).toBe("abandoned");
    expect(read).not.toHaveBeenCalled();
    await expect(
      awaitReviewWrite(
        queued,
        async () => {
          throw Error("offline");
        },
        () => true,
        async () => {},
      ),
    ).rejects.toThrow("offline");
  });
});
