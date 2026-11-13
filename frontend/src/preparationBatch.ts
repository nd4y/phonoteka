export type EvidencePreparation = {
  token: string;
  kind: "recognize" | "analyze";
  reason: string;
  fingerprint_requests_remaining?: number | null;
};

export function preparationBatch<T extends { preparation?: EvidencePreparation | null }>(
  items: T[],
) {
  const eligible = items.filter((i) => i.preparation);
  const metadata = eligible.filter((i) => i.preparation!.kind === "analyze");
  if (metadata.length) return metadata.slice(0, 50);
  // A small capacity group must not indefinitely postpone existing cache work.
  // Keep one server job kind and the original frozen entries; each record's
  // actual send budget is still enforced independently by the backend.
  const recognition = eligible.filter((i) => i.preparation!.kind === "recognize");
  return [
    ...recognition.filter((i) => i.preparation!.fingerprint_requests_remaining !== 0),
    ...recognition.filter((i) => i.preparation!.fingerprint_requests_remaining === 0),
  ].slice(0, 50);
}
