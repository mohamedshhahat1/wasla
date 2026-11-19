/**
 * FEF-45 (FE-7): a wrong current password answers 401 `current_password_invalid`. Before this the
 * session read every 401 as an expired token - it refreshed, retried, met the same 401 and signed
 * the person out of every tab. The account page needs the refusal on its field instead.
 */
import { describe, expect, it, vi } from "vitest";

import type { ChannelLike, LockManagerLike } from "@wasla/session";

import { FORM_REFUSAL_CODES, WebSession } from "../lib/session/web-session";

const locks: LockManagerLike = { request: (_name, callback) => callback() };

function channel(signedOut: () => void): ChannelLike {
  return {
    postMessage: (message: unknown) => {
      if ((message as { type?: string } | null)?.type === "signed-out") signedOut();
    },
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    close: () => undefined,
  };
}

const GRANT = {
  access_token: "token-1",
  expires_in: 600,
  active_workspace: { id: "w1", slug: "acme", name: "Acme", role: "tenant_owner" },
};

function sessionAnswering(answers: Array<{ status: number; body: unknown }>) {
  const urls: string[] = [];
  const signedOut = vi.fn();
  const fetch = vi.fn((input: RequestInfo | URL) => {
    urls.push(input instanceof Request ? input.url : String(input));
    const next = answers.shift() ?? { status: 500, body: {} };
    return Promise.resolve(
      new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  const session = new WebSession({
    apiOrigin: "http://api.test",
    locks,
    channel: channel(signedOut),
    delay: () => Promise.resolve(),
    fetch,
  });
  return { session, fetch, urls, signedOut };
}

const WRONG = {
  status: 401,
  body: {
    error: { code: "current_password_invalid", message: "The current password is incorrect." },
  },
};

describe("a wrong current password is the form's, not the session's (FEF-45)", () => {
  it("answers the 401 as it is: no refresh, no retry, still signed in", async () => {
    const { session, fetch, urls, signedOut } = sessionAnswering([
      { status: 200, body: GRANT },
      WRONG,
    ]);
    await session.restore();
    const answer = await session.api.POST("/api/v1/auth/password", {
      body: { current_password: "wrong", new_password: "a long new passphrase" },
    });
    expect(answer.response.status).toBe(401);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(urls.filter((url) => url.includes("/session/refresh"))).toHaveLength(1);
    expect(session.status).toBe("signed-in");
    expect(signedOut).not.toHaveBeenCalled();
  });

  it("any other 401 still refreshes once and retries (SB-01 unchanged)", async () => {
    const expired = { status: 401, body: { error: { code: "unauthenticated", message: "x" } } };
    const { session, fetch } = sessionAnswering([
      { status: 200, body: GRANT },
      expired,
      { status: 200, body: GRANT },
      { status: 200, body: { locale: "ar" } },
    ]);
    await session.restore();
    const answer = await session.api.GET("/api/v1/auth/me");
    expect(answer.response.status).toBe(200);
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("names exactly one form refusal", () => {
    expect([...FORM_REFUSAL_CODES]).toEqual(["current_password_invalid"]);
  });
});
