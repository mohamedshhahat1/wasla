/**
 * Starting a hosted payment (CK-01 .. CK-04). The page leaves the app by a top-level navigation to
 * the URL the API returned - never a fetch, a frame or a script - and only after this check: an
 * absolute https URL, or, in the e2e hooks build alone, http on localhost (the fake Paymob page).
 * The URL is never rendered, logged, stored or put in history state; the idempotency key lives in
 * the press's closure and dies with it.
 */
export function checkoutUrlAllowed(raw: unknown, options: { e2eHooks: boolean }): boolean {
  if (typeof raw !== "string" || raw === "") return false;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.username || url.password) return false;
  if (url.protocol === "https:") return true;
  return options.e2eHooks && url.protocol === "http:" && url.hostname === "localhost";
}

/** A key for one attempt: a fresh one per press, so "Try again" is a new attempt (RT-05). */
export function newAttemptKey(random: () => string = () => crypto.randomUUID()): string {
  return random();
}
