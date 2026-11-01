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
