import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { JobsView } from "./JobsView";
import type { Job } from "./types";

const job: Job = {
  id: "cancelled-job",
  kind: "analyze",
  state: "cancelled",
  stage: "cancelled",
  total: 10,
  completed: 0,
  failed: 0,
  progress: 0,
  cancel_requested: true,
  created_at: "",
  updated_at: "",
  error: "cancelled",
};
const render = (value: Job) =>
  renderToStaticMarkup(
    <JobsView jobs={[value]} refresh={async () => {}} onCancel={async () => {}} />,
  );

describe("cancelled task presentation", () => {
  it("separates unchanged inventory from actual scan attempts", () => {
    const html = render({
      ...job,
      kind: "scan",
      state: "succeeded",
      stage: "succeeded",
      total: 0,
      completed: 0,
      failed: 0,
      progress: 100,
      cancel_requested: false,
      error: null,
      scan_inventory: { mode: "incremental", discovered: 25, unchanged: 25 },
    });
    expect(html).toContain("Сканирование изменений");
    expect(html).toContain("Без повторного чтения: 25");
    expect(html).toContain("Изменений для проверки нет");
    expect(html).toContain('value="100"');
    expect(html).not.toContain("25 выполнено");
  });
  it("shows an ordinary cancellation without a red error alert", () => {
    const html = render(job);
    expect(html).toContain("Отменено");
    expect(html).toContain("job-card--cancelled");
    expect(html).not.toContain('role="alert"');
    expect(html).not.toContain(">cancelled<");
  });
  it("keeps real failures visible even when cancellation followed completed items", () => {
    const html = render({
      ...job,
      state: "cancelled_partial",
      completed: 2,
      failed: 1,
      error: "invalid_audio",
    });
    expect(html).toContain("Отменено частично");
    expect(html).toContain("invalid_audio");
    expect(html).toContain('role="alert"');
    expect(html).toContain("ошибок или пропусков");
  });
  it("allows cancelling the remaining work of an interrupted task without offering resume", () => {
    const html = render({
      ...job,
      state: "interrupted",
      stage: "interrupted",
      completed: 2,
      cancel_requested: false,
      error: "worker_restarted",
    });
    expect(html).toContain("Отменить оставшиеся файлы");
    expect(html).not.toContain(">Продолжить<");
    expect(html).not.toContain(">Пауза<");
  });
  it("offers continuation only for a failed task with unstarted work", () => {
    const failed = {
      ...job,
      state: "failed",
      stage: "failed",
      cancel_requested: false,
    };
    expect(render({ ...failed, resume_remaining: true })).toContain("Продолжить оставшиеся файлы");
    expect(render(failed)).not.toContain("Продолжить оставшиеся файлы");
  });
});
