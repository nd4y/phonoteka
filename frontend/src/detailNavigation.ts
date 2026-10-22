type DetailSequence = { current: number };

// Declining draft loss keeps the pending detail request valid.
export function acceptDetailNavigation(sequence: DetailSequence, approve: () => boolean) {
  if (!approve()) return false;
  sequence.current += 1;
  return true;
}

export async function loadLatestResult<T>(sequence: DetailSequence, load: () => Promise<T>,
  commit: (value: T) => void, failed: (error: unknown) => void, settled: () => void) {
  const current = ++sequence.current;
  try {
    const value = await load();
    if (current !== sequence.current) return false;
    commit(value);
    return true;
  } catch (error) {
    if (current === sequence.current) failed(error);
    return false;
  } finally {
    if (current === sequence.current) settled();
  }
}

// Polling may overlap a manual refresh. Accept useful completed snapshots while
// a newer one is still pending, but never roll back a snapshot already shown.
export async function loadOrderedResult<T>(sequence: { issued: number; committed: number }, load: () => Promise<T>,
  commit: (value: T) => void, failed: (error: unknown) => void) {
  const current = ++sequence.issued;
  try {
    const value = await load();
    if (current <= sequence.committed) return;
    sequence.committed = current; commit(value);
  } catch (error) {
    if (current > sequence.committed) { sequence.committed = current; failed(error); }
  }
}
