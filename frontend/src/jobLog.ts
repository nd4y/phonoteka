import type { ActivityEvent } from "./jobs";

const problemStates = new Set([
  "failed",
  "conflict",
  "needs_review",
  "interrupted",
  "waiting_authorization",
  "deferred",
]);
export function isProblemEvent(event: ActivityEvent) {
  return problemStates.has(event.state || "") || event.kind === "error";
}
export function filterJobEvents(events: ActivityEvent[], query: string, problems: boolean) {
  const needle = query.trim().toLocaleLowerCase("ru-RU");
  return events.filter(
    (event) =>
      (!problems || isProblemEvent(event)) &&
      (!needle ||
        [event.message, event.filename, event.code, event.provider].some((value) =>
          value?.toLocaleLowerCase("ru-RU").includes(needle),
        )),
  );
}
