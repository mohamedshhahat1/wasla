/**
 * The tab's storage for FE-7's one one-word flag (ADR-201): which signed-in Google flow a return to
 * /auth/google/callback finishes ("link" / "reauth"), removed as it is read. A word the page could
 * print - never a code, a state, a token, a handle or an address - and this is the only file
 * outside packages/session and the composer's draft store that reaches sessionStorage.
 */
export function tabFlags(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}
