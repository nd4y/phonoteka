import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { readPlanPage } from "./readPlanPage";

const timeout = () => new ApiError("Timed out", 408, "read_timeout");

describe("plan page read recovery", () => {
  it("recovers the same timed-out page and preserves the collected prefix", async () => {
    const collected = ["first page"];
    const read = vi
      .fn<() => Promise<string[]>>()
      .mockRejectedValueOnce(timeout())
      .mockResolvedValue(["next page"]);
    const retry = vi.fn();
    collected.push(...(await readPlanPage(read, () => true, retry)));
    expect(collected).toEqual(["first page", "next page"]);
    expect(read).toHaveBeenCalledTimes(2);
    expect(retry).toHaveBeenCalledTimes(1);
  });
  it("stops after the second timeout", async () => {
    const error = timeout();
    const read = vi.fn().mockRejectedValue(error);
    await expect(readPlanPage(read, () => true, vi.fn())).rejects.toBe(error);
    expect(read).toHaveBeenCalledTimes(2);
  });
  it.each([
    new ApiError("Sign in", 401),
    new ApiError("Denied", 403),
    new ApiError("Changed root", 409, "root_identity_changed"),
    new ApiError("Server timeout", 408, "another_code"),
    new Error("Network error"),
  ])("does not repeat non-recoverable reads: %s", async (error) => {
    const read = vi.fn().mockRejectedValue(error);
    const retry = vi.fn();
    await expect(readPlanPage(read, () => true, retry)).rejects.toBe(error);
    expect(read).toHaveBeenCalledTimes(1);
    expect(retry).not.toHaveBeenCalled();
  });
  it("does not resume a stopped or superseded scan", async () => {
    const error = timeout();
    const read = vi.fn().mockRejectedValue(error);
    const retry = vi.fn();
    await expect(readPlanPage(read, () => false, retry)).rejects.toBe(error);
    expect(read).toHaveBeenCalledTimes(1);
    expect(retry).not.toHaveBeenCalled();
  });
  it("rechecks cancellation before admitting the retry", async () => {
    let active = true;
    const error = timeout();
    const read = vi.fn().mockRejectedValue(error);
    await expect(
      readPlanPage(
        read,
        () => active,
        () => {
          active = false;
        },
      ),
    ).rejects.toBe(error);
    expect(read).toHaveBeenCalledTimes(1);
  });
});
