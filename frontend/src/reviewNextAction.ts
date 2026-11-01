export function reviewNextAction(input: {
  changed: boolean;
  savedProposal: boolean;
  invalidLyrics: boolean;
}): "save" | "write" | "lyrics" | "find" {
  if (input.changed) return "save";
  if (!input.savedProposal) return "find";
  return input.invalidLyrics ? "lyrics" : "write";
}
