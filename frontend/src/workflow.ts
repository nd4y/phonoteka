import type { Job } from "./types";
import { isActiveJob } from "./jobs";
import { countValue } from "./CollectionOverview";

export type WorkflowAction = "guide" | "jobs" | "review" | "ready" | "errors";
export function nextCollectionAction(counts: Record<string, number>, jobs: Job[]): WorkflowAction {
  if (!counts.files) return "guide";
  if (jobs.some(isActiveJob)) return "jobs";
  if (counts.current_proposals > 0) return "review";
  if (counts.audit_errors > 0) return "errors";
  if (counts.published_current > 0) return "ready";
  return "guide";
}

// A publication count is distinct from field completeness. Do not add overlapping
// counts, clamp an inconsistent snapshot, or declare an empty library complete.
export function publicationProgress(counts: Record<string, number>) {
  const total = countValue(counts.audio_files);
  const ready = countValue(counts.published_current);
  return {
    total,
    ready,
    percent:
      total !== null && total > 0 && ready !== null && ready <= total
        ? Math.floor((ready / total) * 100)
        : null,
  };
}

export function nextReviewId(ids: string[], currentId: string): string | null {
  const index = ids.indexOf(currentId);
  return index >= 0 ? ids[index + 1] || null : null;
}

export type ReviewWriteResult = "written" | "failed" | "pending" | "abandoned";
// Never advance on task admission, a zero-item success, or an unknown outcome.
// The caller retains the job ID while pending, so checking again cannot write twice.
export async function awaitReviewWrite(
  initial: Job,
  read: (id: string) => Promise<Job>,
  current: () => boolean,
  pause: () => Promise<void> = () => new Promise((resolve) => setTimeout(resolve, 1000)),
  attempts = 30,
): Promise<ReviewWriteResult> {
  let job = initial;
  for (let index = 0; index <= attempts; index++) {
    if (!current()) return "abandoned";
    if (job.state === "succeeded")
      return job.completed === 1 && job.failed === 0 ? "written" : "failed";
    if (!isActiveJob(job)) return "failed";
    if (
      (index > 0 &&
        (job.state === "paused" ||
          job.state === "waiting_authorization" ||
          job.stage === "waiting_storage" ||
          job.stage === "waiting_authorization")) ||
      index === attempts
    )
      return "pending";
    await pause();
    if (!current()) return "abandoned";
    job = await read(job.id);
  }
  return "pending";
}
