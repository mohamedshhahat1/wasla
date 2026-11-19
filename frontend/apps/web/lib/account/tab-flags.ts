/**
 * The tab's storage for FE-7's two one-word flags (ADR-201): which signed-in Google flow a return
 * to /auth/google/callback finishes ("link" / "reauth"), and that this tab already matched its
 * language to the account. Both are words the page could print - never a code, a state, a token,
 * a handle, an address or a language - and this is the only file outside packages/session and the
 * composer's draft store that reaches sessionStorage (the no-web-storage allow-list).
 */
export function tabFlags(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}
