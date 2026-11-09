import { afterEach, expect, it, vi } from "vitest";
import { startStatusPolling } from "./statusPolling";

afterEach(() => vi.useRealTimers());

it("skips overlapping and hidden polls, then resumes and cleans up", async () => {
  vi.useFakeTimers();
  let finish!: () => void;
  let visible = true;
  const refresh = vi
    .fn()
    .mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    )
    .mockResolvedValue(undefined);
  const stop = startStatusPolling(refresh, () => visible);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(12000);
  expect(refresh).toHaveBeenCalledTimes(1);
  visible = false;
  finish();
  await vi.advanceTimersByTimeAsync(8000);
  expect(refresh).toHaveBeenCalledTimes(1);
  visible = true;
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);
  stop();
  await vi.advanceTimersByTimeAsync(8000);
  expect(refresh).toHaveBeenCalledTimes(2);
});
