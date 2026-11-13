import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { startPreparationPolling } from "./preparationPolling";

let visibility: EventTarget & { hidden: boolean };
let stop: (() => void) | undefined;
beforeEach(() => {
  vi.useFakeTimers();
  visibility = Object.assign(new EventTarget(), { hidden: false });
});
afterEach(() => {
  stop?.();
  stop = undefined;
  vi.useRealTimers();
});

it("polls idle preparation every thirty seconds", async () => {
  const refresh = vi.fn().mockResolvedValue(undefined);
  stop = startPreparationPolling(refresh, false, visibility);
  await vi.advanceTimersByTimeAsync(29000);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1000);
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("skips hidden requests and refreshes immediately when visible", async () => {
  visibility.hidden = true;
  const refresh = vi.fn().mockResolvedValue(undefined);
  stop = startPreparationPolling(refresh, false, visibility);
  await vi.advanceTimersByTimeAsync(30000);
  expect(refresh).not.toHaveBeenCalled();
  visibility.hidden = false;
  visibility.dispatchEvent(new Event("visibilitychange"));
  expect(refresh).toHaveBeenCalledTimes(1);
});

it("keeps active progress frequent and stops after disposal", async () => {
  const refresh = vi.fn().mockResolvedValue(undefined);
  stop = startPreparationPolling(refresh, true, visibility);
  await vi.advanceTimersByTimeAsync(5000);
  expect(refresh).toHaveBeenCalledTimes(2);
  stop();
  await vi.advanceTimersByTimeAsync(30000);
  visibility.dispatchEvent(new Event("visibilitychange"));
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("does not overlap requests and aborts an outstanding request on disposal", async () => {
  const refresh = vi.fn((_signal: AbortSignal) => new Promise<void>(() => {}));
  stop = startPreparationPolling(refresh, true, visibility);
  await vi.advanceTimersByTimeAsync(30000);
  visibility.dispatchEvent(new Event("visibilitychange"));
  expect(refresh).toHaveBeenCalledTimes(1);
  const signal = refresh.mock.calls[0][0];
  stop();
  expect(signal.aborted).toBe(true);
});

it("backs off active failures and retries successfully", async () => {
  const refresh = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  stop = startPreparationPolling(refresh, true, visibility);
  await vi.advanceTimersByTimeAsync(9000);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1000);
  expect(refresh).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(5000);
  expect(refresh).toHaveBeenCalledTimes(3);
});
