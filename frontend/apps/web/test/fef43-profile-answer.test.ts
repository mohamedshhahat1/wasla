/**
 * FEF-43 (FE-7, FL-04): a /auth/me answer is filed only under the session state it was asked for.
 * The wrong decision it guards against: a withdrawn member's stale, failed answer overwriting the
 * next state's profile with "none" - read by PageGate as unverified, so /verify (M-Q41).
 */
import { describe, expect, it } from "vitest";

import { decideRoute } from "../lib/routing";
import { answerIsCurrent, failureIsAnAnswer } from "../lib/session/profile-answer";

describe("a profile answer belongs to the state it was asked for", () => {
  it("an answer for the current state is kept", () => {
    expect(answerIsCurrent("signed-in|4", "signed-in|4")).toBe(true);
  });

  it("an answer for an earlier generation is dropped", () => {
    expect(answerIsCurrent("signed-in|4", "signed-in|5")).toBe(false);
    expect(answerIsCurrent("loading|0", "signed-in|1")).toBe(false);
  });

  it("the decision the stale answer used to cause: none as unverified sends to /verify", () => {
    // What PageGate computed when a stale failure was filed under the new state.
    expect(
      decideRoute(
        "workspace",
        { status: "signed-in", verified: false, workspaceCount: 0 },
        "/contacts",
      ),
    ).toEqual({ kind: "go", to: "/verify" });
    // What it computes now: the new state's own profile (verified, no workspace left).
    expect(
      decideRoute(
        "workspace",
        { status: "signed-in", verified: true, workspaceCount: 0 },
        "/contacts",
      ),
    ).toEqual({ kind: "go", to: "/workspaces/new" });
  });
});

describe("a failed /auth/me that never got an answer is not a profile (FEF-43, WebKit)", () => {
  it("network_error - an aborted fetch on navigation - is not filed", () => {
    expect(failureIsAnAnswer("network_error")).toBe(false);
  });

  it("an answer from the API is filed as before", () => {
    expect(failureIsAnAnswer("unauthenticated")).toBe(true);
    expect(failureIsAnAnswer("internal_error")).toBe(true);
    expect(failureIsAnAnswer(null)).toBe(true);
  });
});
