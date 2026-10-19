// The owner already approved discarding drafts before PWA activation.
// Permit only its immediate reload; ordinary navigation keeps its warning.
export function createPwaReloadGuard() {
  let approvedReload = false;
  return {
    prepareReload(saveCurrentState: () => void) {
      approvedReload = false;
      saveCurrentState();
      approvedReload = true;
    },
    reset() { approvedReload = false; },
    beforeUnload(event: Pick<BeforeUnloadEvent, 'preventDefault' | 'returnValue'>, dirty: boolean) {
      if (approvedReload) { approvedReload = false; return; }
      if (dirty) { event.preventDefault(); event.returnValue = ''; }
    },
  };
}
