/**
 * The composer draft's store (CP-01; plan section 21: "in memory + sessionStorage, cleared on send
 * and sign-out"). The one file of apps/web allowed to touch web storage (STORAGE_ALLOW_FILES): it
 * hands out sessionStorage and nothing else - never localStorage, never a token. What is written
 * and when is decided in composer.ts (draftKey / writeDraft / clearDrafts).
 */
import type { StorageLike } from "./composer";

export function draftStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    // Storage blocked (private mode, a policy): the draft lives in memory only.
    return null;
  }
}
