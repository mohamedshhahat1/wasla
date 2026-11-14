/**
 * SU-04 (FEF-24, ADR-197): what an answer of the refresh means for the session, in one table
 * shared by the browser session and the /session/refresh route, so the two cannot disagree.
 *
 * A suspended workspace is not a signed-out person: the refresh into it is refused with 403
 * `workspace_suspended`, the API leaves the refresh token unspent, and the session keeps the
 * person signed in on the suspended state ("Check again" refreshes again). Every other refusal
 * signs out as it always did.
 */
export type RefreshOutcome =
  "signed-in" | "signed-out" | "workspace-suspended" | "transient" | "error";

/** The refusals that keep the session (and its cookie): the person is signed in, the workspace is not usable. */
export const SESSION_KEEPING_REFUSALS: ReadonlySet<string> = new Set(["workspace_suspended"]);

export function refreshOutcome(status: number, code: string | null): RefreshOutcome {
  if (status >= 200 && status < 300) return "signed-in";
  if (status === 403 && code !== null && SESSION_KEEPING_REFUSALS.has(code)) {
    return "workspace-suspended";
  }
  if (status === 401 || status === 403) return "signed-out";
  if (status === 0 || status >= 500) return "transient";
  return "error";
}

/** Whether the refresh route clears the refresh cookie for this answer: only when it signs out. */
export function refreshClearsCookie(status: number, code: string | null): boolean {
  return refreshOutcome(status, code) === "signed-out";
}
