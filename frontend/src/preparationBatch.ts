export type EvidencePreparation = {
  token: string;
  kind: "recognize" | "analyze";
  reason: string;
  fingerprint_requests_remaining?: number | null;
};

// Metadata search uses existing evidence first. Expensive cache-only audio
// checks follow work which can still obtain new recognition evidence.
function priority(value: EvidencePreparation) {
  return value.kind === "analyze" ? 0 : value.fingerprint_requests_remaining === 0 ? 2 : 1;
}
export function preparationBatch<T extends { preparation?: EvidencePreparation | null }>(
  items: T[],
) {
  const eligible = items.filter((i) => i.preparation);
  const next = eligible.reduce((best, i) => Math.min(best, priority(i.preparation!)), 3);
  return eligible.filter((i) => priority(i.preparation!) === next).slice(0, 50);
}
