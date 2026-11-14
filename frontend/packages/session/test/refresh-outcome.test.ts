/* eslint-disable @typescript-eslint/require-await -- test doubles implement async interfaces without awaiting */
/** SU-04 (FEF-24): what each refresh answer means, row by row; a suspension keeps the session. */
import { describe, expect, it } from "vitest";

import {
  refreshClearsCookie,
  refreshOutcome,
  SignedOut,
  TransientFailure,
  withBackoff,
  WorkspaceSuspended,
  type RefreshOutcome,
} from "../src/browser";

const TABLE: Array<[number, string | null, RefreshOutcome, boolean]> = [
  [200, null, "signed-in", false],
  [401, "unauthenticated", "signed-out", true],
  [401, "invalid_token", "signed-out", true],
  [401, null, "signed-out", true],
  [403, "workspace_suspended", "workspace-suspended", false],
  [403, "membership_revoked", "signed-out", true],
  [403, "session_request_refused", "signed-out", true],
  [403, null, "signed-out", true],
  [404, "not_found", "error", false],
  [429, "rate_limited", "error", false],
  [0, null, "transient", false],
  [502, "unexpected_response", "transient", false],
  [503, "session_unavailable", "transient", false],
];

describe("the refresh outcome table (SU-04, FEF-24)", () => {
  it.each(TABLE)("%i %s → %s (clears the cookie: %s)", (status, code, outcome, clears) => {
    expect(refreshOutcome(status, code)).toBe(outcome);
    expect(refreshClearsCookie(status, code)).toBe(clears);
  });

  it("a suspension during backoff ends at once, not as a sign-out and not retried", async () => {
    let calls = 0;
    const waits: number[] = [];
    await expect(
      withBackoff(
        async () => {
          calls += 1;
          if (calls === 1) throw new TransientFailure(0);
          throw new WorkspaceSuspended();
        },
        {
          clock: { now: () => 0 },
          delay: async (ms: number) => {
            waits.push(ms);
          },
          expiresAt: () => 900_000,
        },
      ),
    ).rejects.toBeInstanceOf(WorkspaceSuspended);
    expect(calls).toBe(2);
    expect(waits).toEqual([1000]);
    expect(new WorkspaceSuspended()).not.toBeInstanceOf(SignedOut);
  });
});
