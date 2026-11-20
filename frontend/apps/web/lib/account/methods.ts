/**
 * FEB-45 (AC8-01 .. AC8-03): which account controls apply, from the two facts GET /auth/me
 * reports - `has_password` and `google_connected` - and whether Google sign-in is offered at all.
 * One password mode, one Google action, and exactly the proofs that can close the account.
 */
export interface SignInFacts {
  hasPassword: boolean;
  googleConnected: boolean;
}

export interface AccountModes {
  password: "change" | "set";
  /** "disconnect-blocked": connected, but without a password disconnecting would lock them out. */
  google: "connect" | "disconnect" | "disconnect-blocked" | "hidden";
  closeProofs: Array<"password" | "google">;
}

export function accountModes(facts: SignInFacts, googleOffered: boolean): AccountModes {
  const google: AccountModes["google"] = facts.googleConnected
    ? facts.hasPassword
      ? "disconnect"
      : "disconnect-blocked"
    : googleOffered
      ? "connect"
      : "hidden";
  const closeProofs: AccountModes["closeProofs"] = [];
  if (facts.hasPassword) closeProofs.push("password");
  if (facts.googleConnected && googleOffered) closeProofs.push("google");
  return { password: facts.hasPassword ? "change" : "set", google, closeProofs };
}

/** The facts from a profile; an older answer without them is read as a password account. */
export function signInFacts(
  profile: { has_password?: boolean; google_connected?: boolean } | null | undefined,
): SignInFacts {
  return {
    hasPassword: profile?.has_password ?? true,
    googleConnected: profile?.google_connected ?? false,
  };
}
