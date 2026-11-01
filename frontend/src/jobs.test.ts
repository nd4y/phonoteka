import { describe, expect, it } from "vitest";
import { etaText, isActiveJob, jobTone, mergeActivityEvents, needsJobAuthorization } from "./jobs";
import type { ActivityEvent, LiveJob } from "./jobs";

const job: LiveJob = {
  id: "test",
  kind: "recognize",
  state: "running",
  stage: "recognize",
  total: 10,
  completed: 2,
  failed: 0,
  progress: 20,
  cancel_requested: false,
  created_at: "",
  updated_at: "",
};
const event = (id: number): ActivityEvent => ({
  id,
  kind: "job_progress",
  created_at: "",
  message: `Event ${id}`,
});

describe("job progress presentation", () => {
  it("keeps cancellation visually distinct from success and error", () => {
    expect(jobTone({ ...job, state: "succeeded" })).toBe("success");
    expect(jobTone({ ...job, state: "cancelled" })).toBe("cancelled");
    expect(jobTone({ ...job, state: "cancelled_partial", failed: 1 })).toBe("cancelled");
    expect(jobTone({ ...job, state: "completed_with_errors", failed: 1 })).toBe("error");
    expect(jobTone({ ...job, stage: "waiting_provider", state: "queued" })).toBe("waiting");
    expect(jobTone({ ...job, state: "paused" })).toBe("paused");
    expect(jobTone({ ...job, state: "conflict", failed: 0, stage: "waiting_storage" })).toBe(
      "error",
    );
    expect(jobTone({ ...job, state: "needs_review", stage: "waiting_provider" })).toBe("error");
    expect(
      jobTone({ ...job, state: "succeeded", stage: "waiting_storage", pause_requested: true }),
    ).toBe("success");
  });
  it("explains unknown ETA and never invents a deadline for a provider wait", () => {
    expect(etaText({ ...job, eta_seconds: null, eta_reason: "waiting_provider" })).toBe(
      "ETA после ответа сервиса",
    );
    expect(etaText({ ...job, eta_seconds: null, eta_reason: "insufficient_samples" })).toBe(
      "Собираем данные для ETA",
    );
    expect(etaText({ ...job, eta_seconds: 125 })).toBe("Осталось около 3 мин");
    expect(etaText({ ...job, eta_seconds: Infinity })).toBe("Собираем данные для ETA");
  });
  it("keeps authorization waiting active and distinct from pause, provider wait and cancellation", () => {
    const waiting = {
      ...job,
      state: "waiting_authorization",
      stage: "waiting_authorization",
      eta_seconds: 125,
      pause_requested: true,
    };
    expect(isActiveJob(waiting)).toBe(true);
    expect(jobTone(waiting)).toBe("authorization");
    expect(etaText(waiting)).toBe("ETA после подтверждения");
    expect(jobTone({ ...waiting, cancel_requested: true })).toBe("cancelled");
    expect(etaText({ ...waiting, cancel_requested: true })).toBe("Завершаем текущие операции");
    expect(jobTone({ ...waiting, state: "cancelled" })).toBe("cancelled");
    expect(needsJobAuthorization({ ...waiting, state: "running" })).toBe(true);
    expect(needsJobAuthorization({ ...waiting, state: "completed" })).toBe(false);
    expect(jobTone({ ...waiting, state: "completed" })).toBe("success");
  });
  it("deduplicates replay after reconnect, orders events, and bounds memory to 200 lines", () => {
    const previous = Array.from({ length: 200 }, (_, index) => event(index + 1));
    const result = mergeActivityEvents(previous, [
      event(199),
      event(201),
      event(202),
      event(0),
      event(NaN),
    ]);
    expect(result).toHaveLength(200);
    expect(result[0].id).toBe(3);
    expect(result.at(-1)?.id).toBe(202);
    expect(result.filter((item) => item.id === 199)).toHaveLength(1);
  });
});
