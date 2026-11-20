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

/**
 * FEF-43, the broader half (frontend.yml 44638302967, WebKit): whether a failed /auth/me says
 * anything about the account. A request that never got an answer - WebKit aborts the page's
 * fetches as it navigates away and rejects them as `network_error` - is not "no profile"; filed
 * as one, PageGate read it as an unverified account and sent the page to /verify mid-navigation.
 * Such a failure is not filed: the page keeps waiting (or leaves), and the session's own state
 * says when the connection itself is lost.
 */
export function failureIsAnAnswer(code: string | null | undefined): boolean {
  return code !== "network_error";
}
