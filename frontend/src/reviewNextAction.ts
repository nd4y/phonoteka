export function reviewNextAction(input: {
  changed: boolean;
  savedProposal: boolean;
  invalidLyrics: boolean;
  canRecover?: boolean;
}): "save" | "write" | "lyrics" | "find" | "recover" {
  if (input.changed) return "save";
  if (input.canRecover) return "recover";
  if (!input.savedProposal) return "find";
  return input.invalidLyrics ? "lyrics" : "write";
}
