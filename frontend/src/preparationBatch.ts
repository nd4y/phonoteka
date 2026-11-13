export type EvidencePreparation = {
  token: string;
  kind: "recognize" | "analyze";
  reason: string;
  fingerprint_requests_remaining?: number | null;
};

export function preparationBatch<T extends { preparation?: EvidencePreparation | null }>(
  items: readonly T[],
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

type PreparationEntry = {
  track_id: string;
  preparation?: EvidencePreparation | null;
};

export type PreparationSelection = {
  plan: readonly PreparationEntry[];
  tokens: ReadonlyMap<string, string>;
};

export function preparationSelection(items: readonly PreparationEntry[]): PreparationSelection {
  return {
    plan: items,
    tokens: new Map(preparationBatch(items).map((i) => [i.track_id, i.preparation!.token])),
  };
}

export function selectedPreparationBatch<T extends PreparationEntry>(
  items: readonly T[],
  selection: PreparationSelection | null,
) {
  const batch = preparationBatch(items);
  // A new plan starts with its own default selection, even when IDs are unchanged.
  if (!selection || selection.plan !== items) return batch;
  return batch.filter((i) => selection.tokens.get(i.track_id) === i.preparation!.token);
}

export function setPreparationSelected(
  items: readonly PreparationEntry[],
  selection: PreparationSelection | null,
  trackId: string,
  checked: boolean,
): PreparationSelection {
  const current = selection?.plan === items ? selection : preparationSelection(items);
  const entry = preparationBatch(items).find((i) => i.track_id === trackId);
  if (!entry) return current;
  const tokens = new Map(current.tokens);
  if (checked) tokens.set(trackId, entry.preparation!.token);
  else tokens.delete(trackId);
  return { plan: items, tokens };
}
