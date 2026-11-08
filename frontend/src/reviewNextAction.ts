export function reviewNextAction(input: {
  changed: boolean;
  savedProposal: boolean;
  invalidLyrics: boolean;
  canRecover?: boolean;
  preparation?: boolean;
  possibleMix?: boolean;
  fileProblem?: boolean;
}): "save" | "write" | "lyrics" | "find" | "recover" | "prepare" | "recording" | "file" {
  if (input.changed) return "save";
  if (input.canRecover) return "recover";
  if (input.preparation) return "prepare";
  if (input.fileProblem) return "file";
  if (input.possibleMix) return "recording";
  if (!input.savedProposal) return "find";
  return input.invalidLyrics ? "lyrics" : "write";
}
