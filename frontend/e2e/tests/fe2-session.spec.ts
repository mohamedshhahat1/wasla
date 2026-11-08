/**
 * FE-2's session journeys against FE-0's real API (JT-03..JT-05):
 *   J6  two tabs refresh together 50 times: one /auth/refresh per round, nobody signed out; a
 *       third tab joins by broadcast without refreshing; the control without the lock IS
 *       signed out (the test can fail)
 *   J7  switching in one tab switches every tab; the old workspace's resource is 404
 *   J3  the invitee's half: the mailed link → a new account → verify → member of the workspace
 *   J9  Google through the relay with the fake Google; cancelled; no binding cookie
 * In en and ar, in every project. After every step: nothing reachable (JT-06).
 */
import type { BrowserContext, Page } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import {
  field,
  api,
  label,
  lead,
  logInThroughTheApp,
  nothingReachable,
  PASSWORD,
  REFRESH_COOKIES,
  testAddress,
  useAddress,
  useLocale,
  verifiedOwner,
  waitForCode,
  waitForLink,
  WEB,
} from "../support/fe2";
import { freshEmail } from "../support/helpers";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });

const LOCALES: Locale[] = ["en", "ar"];
const FAKES = "http://localhost:8026";

interface Hooks {
  __waslaRefreshNow(): Promise<string>;
  __waslaRefreshNoLock(): Promise<string>;
  __waslaToken(): string | null;
  __waslaMe(): Promise<number>;
}

async function hooksPage(context: BrowserContext): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`${WEB}/test-only/session`);
  const marker = page.getByTestId("session-hooks");
  await expect(marker).toHaveAttribute("data-ready", "yes");
  await expect(marker).toHaveAttribute("data-status", "signed-in");
  return page;
}

const refreshNow = (page: Page) =>
  page.evaluate(() => (window as unknown as Hooks).__waslaRefreshNow());
const tokenOf = (page: Page) => page.evaluate(() => (window as unknown as Hooks).__waslaToken());
const meStatus = (page: Page) => page.evaluate(() => (window as unknown as Hooks).__waslaMe());

/**
 * Holds every /session/refresh answer for 250 ms (a slower network), so the second tab's call
 * always lands while the first one is in flight: the race J6 is about, every round, rather than
 * whenever Playwright happens to dispatch both evaluations close enough (JT-03). Each refresh is
 * signed under a new address: the API counts refreshes with sign-ins, 10 a minute per address
 * (AuthRateLimit), and 50 rounds are about the race, not that limit (SR-06 proves the limit).
 */
async function slowRefresh(context: BrowserContext): Promise<void> {
  await context.route(`${WEB}/session/refresh`, async (route) => {
    const headers = { ...route.request().headers(), "x-wasla-e2e-client-ip": testAddress() };
    const response = await route.fetch({ headers });
    await new Promise((resolve) => setTimeout(resolve, 250));
    // A sign-out (the control) navigates the tab away and cancels its request: nothing to answer.
    await route.fulfill({ response }).catch(() => undefined);
  });
}

function countRefreshes(context: BrowserContext): { value: number } {
  const counter = { value: 0 };
  context.on("request", (request) => {
    if (request.url() === `${WEB}/session/refresh` && request.method() === "POST")
      counter.value += 1;
  });
  return counter;
}

for (const locale of LOCALES) {
  test.describe(`${locale}`, () => {
    test(`J6: two tabs refresh at once 50 times; nobody is signed out; a third tab joins`, async ({
      page,
      context,
    }) => {
      test.setTimeout(240_000);
      const owner = await verifiedOwner("j6");
      await useAddress(context);
      await useLocale(context, locale);
      await logInThroughTheApp(page, locale, owner.email);
      await page.waitForURL(`${WEB}/`);
      const first = await hooksPage(context);
      const second = await hooksPage(context);
      await slowRefresh(context);
      const refreshes = countRefreshes(context);

      let third: Page | null = null;
      for (let round = 1; round <= 50; round += 1) {
        const before = refreshes.value;
        const [a, b] = await Promise.all([refreshNow(first), refreshNow(second)]);
        expect(refreshes.value - before, `round ${round}: one refresh`).toBe(1);
        expect(a, `round ${round}: both tabs hold the same token`).toBe(b);
        if (round === 25) {
          const beforeJoin = refreshes.value;
          third = await hooksPage(context);
          expect(await tokenOf(third)).toBe(a);
          expect(refreshes.value - beforeJoin, "the third tab joined without refreshing").toBe(0);
        }
      }
      for (const tab of [first, second, third!]) expect(await meStatus(tab)).toBe(200);
      await nothingReachable(first, context);
      test.info().annotations.push({
        type: "j6",
        description: `50 rounds, ${refreshes.value} refreshes, 0 sign-outs`,
      });
    });

    test(`J6 control: the same race without the lock signs the account out`, async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("j6c");
      await useAddress(context);
      await useLocale(context, locale);
      await logInThroughTheApp(page, locale, owner.email);
      await page.waitForURL(`${WEB}/`);
      const first = await hooksPage(context);
      const second = await hooksPage(context);
      await slowRefresh(context);
      let signedOut = false;
      for (let round = 1; round <= 10 && !signedOut; round += 1) {
        const results = await Promise.allSettled([
          first.evaluate(() => (window as unknown as Hooks).__waslaRefreshNoLock()),
          second.evaluate(() => (window as unknown as Hooks).__waslaRefreshNoLock()),
        ]);
        if (results.some((r) => r.status === "rejected")) signedOut = true;
      }
      expect(signedOut, "without the lock the race must sign the account out").toBe(true);
      // The API revoked the family: a fresh refresh with the lock cannot recover it.
      await expect(refreshNow(first)).rejects.toThrow();
      const jar = (await context.cookies(WEB)).filter((c) => REFRESH_COOKIES.includes(c.name));
      expect(jar).toEqual([]);
    });

    test(`J7: a switch in one tab switches every tab; the old workspace is 404 with the new token`, async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("j7");
      const second = `j7b-${Math.random().toString(36).slice(2, 9)}`;
      const created = await api("POST", "/api/v1/workspaces", {
        token: owner.token,
        address: owner.address,
        body: { name: `Beta ${second}`, slug: second, timezone: "Africa/Cairo" },
      });
      expect(created.status).toBe(201);
      // A resource of the first workspace, made with its token.
      const invited = await api("POST", "/api/v1/invitations", {
        token: owner.token,
        address: owner.address,
        body: { email: freshEmail("j7inv"), role: "member" },
      });
      expect(invited.status).toBe(201);
      const invitationId = (invited.json as { id: string }).id;

      await useAddress(context);
      await useLocale(context, locale);
      await logInThroughTheApp(page, locale, owner.email);
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(`Workspace ${owner.slug}`);
      const other = await context.newPage();
      await other.goto(`${WEB}/`);
      await expect(other.getByTestId("signed-in-as")).toContainText(`Workspace ${owner.slug}`);
      const hooks = await hooksPage(context);
      const oldToken = await tokenOf(hooks);

      await page.goto(`${WEB}/workspaces`);
      await page
        .getByRole("listitem")
        .filter({ hasText: `Beta ${second}` })
        .getByRole("button", { name: label(locale, "auth.workspace.switchTo") })
        .click();
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(`Beta ${second}`);
      await expect(other.getByTestId("signed-in-as")).toContainText(`Beta ${second}`);
      await expect(
        other.getByText(lead(locale, "auth.switchedElsewhere").split("{")[0]!.trim()).first(),
      ).toBeVisible();
      await expect(other.getByTestId("signed-in-as")).not.toContainText(`Workspace ${owner.slug}`);

      const newToken = await tokenOf(hooks);
      expect(newToken).not.toBe(oldToken);
      const stale = await fetch(`${API_ORIGIN}/api/v1/invitations/${invitationId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${newToken}` },
      });
      expect(stale.status).toBe(404);
      await nothingReachable(other, context);
    });

    test(`J3: the invitee accepts the mailed link, verifies, and is a member`, async ({
      page,
      context,
    }) => {
      test.setTimeout(120_000);
      const owner = await verifiedOwner("j3own");
      const invitee = freshEmail("j3");
      const invited = await api("POST", "/api/v1/invitations", {
        token: owner.token,
        address: owner.address,
        body: { email: invitee, role: "member" },
      });
      expect(invited.status).toBe(201);
      const link = await waitForLink(invitee, "/invitations/accept");
      const token = new URL(link).searchParams.get("token")!;

      await useAddress(context);
      await useLocale(context, locale);
      const response = await page.goto(link);
      expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.invitation.title") }),
      ).toBeVisible();
      expect(page.url()).not.toContain(token);
      await nothingReachable(page, context);
      await field(page, locale, "auth.fullName").fill("Invited Person");
      await field(page, locale, "auth.password").fill(PASSWORD);
      await page.getByRole("button", { name: label(locale, "auth.invitation.submit") }).click();

      await page.waitForURL(`${WEB}/verify`);
      // Accepting may not mail a code (FEB-17: the invitee is unverified); after 5 s, ask for one.
      let code: string;
      try {
        code = await waitForCode(invitee, new Set(), 10);
      } catch {
        await page.getByRole("button", { name: label(locale, "auth.verify.resend") }).click();
        code = await waitForCode(invitee);
      }
      await field(page, locale, "auth.verify.code").fill(code);
      await page.getByRole("button", { name: label(locale, "auth.verify.submit") }).click();
      await page.waitForURL(`${WEB}/`);
      const placeholder = page.getByTestId("signed-in-as");
      await expect(placeholder).toContainText(`Workspace ${owner.slug}`);
      await expect(placeholder).toContainText(label(locale, "auth.roles.member"));
      await nothingReachable(page, context);
    });

    test(`J9: Google through the relay; code and state leave the address bar`, async ({
      page,
      context,
      guard,
    }) => {
      const email = freshEmail("j9");
      const address = await useAddress(context);
      await useLocale(context, locale);
      // The route handler's own Set-Cookie, read where it is fetched (FX-02: from the header).
      const relayed: Array<{ name: string; value: string }> = [];
      // The fake Google signs in whoever ?email= names: one account per test. The app's own
      // /session/google/authorize answer gets the parameter (its Set-Cookie untouched); WebKit
      // cannot fulfil a navigation with the fake's 302, so the navigation itself is left alone.
      await context.route(`${WEB}/session/google/authorize`, async (route) => {
        const headers = { ...route.request().headers(), "x-wasla-e2e-client-ip": address };
        const response = await route.fetch({ headers });
        relayed.push(
          ...response.headersArray().filter((h) => h.name.toLowerCase() === "set-cookie"),
        );
        const body = (await response.json()) as { authorization_url: string };
        await route.fulfill({
          response,
          json: {
            authorization_url: `${body.authorization_url}&email=${encodeURIComponent(email)}`,
          },
        });
      });
      await page.goto(`${WEB}/login`);
      const authorize = page.waitForResponse(`${WEB}/session/google/authorize`);
      await page.getByRole("button", { name: label(locale, "auth.google.continue") }).click();
      await authorize;
      expect(relayed).toHaveLength(1);
      // FEB-21: over plain HTTP the binding cookie is wasla_oauth; the relay keeps the name.
      expect(relayed[0]!.value).toMatch(/^wasla_oauth=[^;]+; .*HttpOnly/);
      test.info().annotations.push({
        type: "binding-cookie",
        description: relayed[0]!.value.split("=")[0]!,
      });

      // Signed in. An example.com address is not one Google is authoritative for, so the API
      // leaves it unverified (GoogleAuthService) and the app routes it to /verify (AU-03).
      await page.waitForURL(`${WEB}/verify`);
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.verify.title") }),
      ).toBeVisible();
      expect(page.url()).not.toMatch(/[?&](code|state)=/);
      expect(
        await page.evaluate(() => JSON.stringify(window.history.state as unknown)),
      ).not.toMatch(/e2e-code-/);
      const jar = await context.cookies(WEB);
      expect(jar.filter((c) => REFRESH_COOKIES.includes(c.name))).toHaveLength(1);
      expect(jar.filter((c) => c.name === "wasla_oauth")).toEqual([]);
      await nothingReachable(page, context);
      for (const origin of guard.origins) expect([WEB, API_ORIGIN, FAKES]).toContain(origin);
      expect(guard.violations).toEqual([]);
    });

    test(`J9: a cancelled Google sign-in and a callback without the binding cookie`, async ({
      page,
      context,
    }) => {
      await useAddress(context);
      await useLocale(context, locale);
      await page.goto(`${WEB}/auth/google/callback?error=access_denied&state=x`);
      await page.waitForURL(/\/login/);
      await expect(page.getByText(label(locale, "auth.google.cancelled"))).toBeVisible();
      expect(page.url()).not.toContain("access_denied");

      await page.goto(`${WEB}/auth/google/callback?code=e2e-code-forged&state=forged`);
      const alert = page.getByRole("alert").first();
      await expect(alert).toBeVisible();
      await expect(alert).toHaveAttribute("data-error-code", /.+/);
      expect(page.url()).not.toContain("e2e-code-forged");
      const jar = (await context.cookies(WEB)).filter((c) => REFRESH_COOKIES.includes(c.name));
      expect(jar).toEqual([]);
    });
  });
}
