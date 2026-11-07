/**
 * Against FE-0's real API (the e2e profile): SS-03 - the TypeScript signer's forward is counted
 * under the signed address (the TypeScript half of FEB-01, end to end) - and one call of each
 * helper FE-2 will use. Runs once, in one project: it exercises the API, not a browser.
 */
import { expect, test } from "@playwright/test";

import { unwrap } from "@wasla/api-client";

import { API_ORIGIN } from "../support/env";
import { e2eApi, freshEmail, mailbox, signedForward } from "../support/helpers";

test.skip(({ browserName, viewport }) => browserName !== "chromium" || viewport?.width !== 1280);

/** A unique documentation-range address per run, so reruns never share a bucket. */
function testAddress(): string {
  const n = (Date.now() % 250) + 1;
  return `198.18.${Math.floor(Math.random() * 250) + 1}.${n}`;
}

async function login(email: string, headers: Record<string, string>) {
  const response = await fetch(`${API_ORIGIN}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify({ email, password: "not-the-password-0" }),
  });
  return response.status;
}

test("a signed login is counted under the signed address; two addresses do not share a bucket", async () => {
  const signedA = testAddress();
  let signedB = testAddress();
  while (signedB === signedA) signedB = testAddress();
  const statusesA: number[] = [];
  for (let i = 0; i < 11; i += 1) {
    // A fresh account each time: the per-account bucket never trips, only the address bucket.
    statusesA.push(
      await login(freshEmail("sig"), signedForward(signedA, "POST", "/api/v1/auth/login")),
    );
  }
  expect(statusesA.slice(0, 10)).toEqual(Array(10).fill(401));
  expect(statusesA[10]).toBe(429);
  // Same machine, another signed address: its own, untouched bucket.
  expect(await login(freshEmail("sig"), signedForward(signedB, "POST", "/api/v1/auth/login"))).toBe(
    401,
  );
  // A forward signed with another secret is ignored: the caller's own address is used, which
  // the previous requests did not spend (they were all counted under the signed addresses).
  const forged = {
    ...signedForward(signedA, "POST", "/api/v1/auth/login"),
    "X-Wasla-Forward-Sig": "0".repeat(64),
  };
  expect(await login(freshEmail("sig"), forged)).toBe(401);
  test.info().annotations.push({ type: "statuses", description: JSON.stringify(statusesA) });
});

test("helpers: the mailbox, fresh emails and the typed client reach the e2e stack", async () => {
  const address = freshEmail("helper");
  expect(address).toMatch(/^helper\.[0-9a-f-]{12}@example\.com$/);
  expect(await mailbox.messagesTo(address)).toEqual([]);
  const api = e2eApi();
  const zones = await unwrap(api.GET("/api/v1/timezones"));
  expect(Array.isArray(zones) || typeof zones === "object").toBe(true);
  const error = await unwrap(api.GET("/api/v1/auth/me")).catch((e: unknown) => e);
  expect(error).toMatchObject({
    status: 401,
    code: "unauthenticated",
    messageKey: "errors.unauthenticated",
  });
});
