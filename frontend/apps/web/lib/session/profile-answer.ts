/**
 * FEF-43 (FE-7, FL-04): whether a /auth/me answer belongs to the session state the page now has.
 *
 * Every session state (status and generation) loads its own profile. An answer asked for under
 * one state can land after the next state's own answer - a withdrawn member's reload refreshes
 * into a new generation while the old generation's /auth/me is still in flight, and that one
 * fails. Filed under the state current when it lands, the stale failure replaced the new state's
 * profile with "none", which PageGate reads as an unverified account: /verify, and no reload ever
 * came to correct it (CJ-10 in WebKit, frontend.yml FE-6 push 1). An answer is kept only for the
 * state it was asked for; a stale one is dropped, and the current state's own load stands.
 */
export function answerIsCurrent(askedFor: string, current: string): boolean {
  return askedFor === current;
}
