/**
 * Which signed-in Google flow a return to /auth/google/callback finishes (SG-02, ADR-201). Every
 * flow comes back to the one fixed redirect URI the API registers, so the page that starts a
 * flow notes its kind here first. sessionStorage holds that word and nothing else - never a code,
 * a state, a token or an address - and the callback page removes it as it reads it.
 */
export type GoogleFlow = "link" | "reauth";

export const GOOGLE_FLOW_KEY = "wasla_google_flow";

export function noteGoogleFlow(storage: Pick<Storage, "setItem"> | null, flow: GoogleFlow): void {
  try {
    storage?.setItem(GOOGLE_FLOW_KEY, flow);
  } catch {
    // No storage: the return is read as a sign-in, which a signed-in person's session survives.
  }
}

export function takeGoogleFlow(
  storage: Pick<Storage, "getItem" | "removeItem"> | null,
): GoogleFlow | null {
  try {
    const flow = storage?.getItem(GOOGLE_FLOW_KEY) ?? null;
    storage?.removeItem(GOOGLE_FLOW_KEY);
    return flow === "link" || flow === "reauth" ? flow : null;
  } catch {
    return null;
  }
}
