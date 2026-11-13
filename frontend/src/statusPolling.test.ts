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

it("uses the idle interval to reduce network calls", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  const refresh = vi.fn().mockResolvedValue(undefined);
  const stop = startStatusPolling(
    refresh,
    () => true,
    () => 30000,
  );

  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(28000);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);

  stop();
});

it("applies a shortened interval on the next eligible 4s tick", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  let interval = 30000;
  const refresh = vi.fn().mockResolvedValue(undefined);
  const stop = startStatusPolling(
    refresh,
    () => true,
    () => interval,
  );

  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(8000);
  expect(refresh).toHaveBeenCalledTimes(1);

  interval = 4000;
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);

  stop();
});

it("refreshes on the next tick after becoming visible again", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  let visible = false;
  const refresh = vi.fn().mockResolvedValue(undefined);
  const stop = startStatusPolling(
    refresh,
    () => visible,
    () => 30000,
  );

  // The initial refresh still happens immediately, as before.
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(1);

  visible = true;
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);

  stop();
});

it("releases pending state after a rejected refresh", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  const refresh = vi
    .fn()
    .mockRejectedValueOnce(new Error("refresh failed"))
    .mockResolvedValue(undefined);
  const stop = startStatusPolling(refresh, () => true);

  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);

  stop();
});

it("stops new calls even when an in-flight refresh resolves after stop", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  let finish!: () => void;
  const refresh = vi.fn().mockReturnValueOnce(
    new Promise<void>((resolve) => {
      finish = resolve;
    }),
  );
  const stop = startStatusPolling(refresh, () => true);

  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(8000);
  expect(refresh).toHaveBeenCalledTimes(1);

  stop();
  finish();
  await vi.advanceTimersByTimeAsync(8000);
  expect(refresh).toHaveBeenCalledTimes(1);
});

it("falls back to the 4s interval when the dynamic interval is not finite", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  let interval = Number.POSITIVE_INFINITY;
  const refresh = vi.fn().mockResolvedValue(undefined);
  const stop = startStatusPolling(
    refresh,
    () => true,
    () => interval,
  );

  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(2);

  interval = Number.NaN;
  await vi.advanceTimersByTimeAsync(4000);
  expect(refresh).toHaveBeenCalledTimes(3);

  stop();
});
