/**
 * SU-07 (FEF-24, ADR-197): a reload of a suspended workspace keeps the person signed in.
 *
 * Before: the refresh into the suspended workspace was refused (403), the session read every
 * refused refresh as "signed out", and the person was sent to the log-in page - and the refusal
 * had spent their refresh token. Now the page shows GT-03's suspended state, the cookie is still
 * there, nothing but the refresh is asked of the API, "Check again" asks again (no replay), and
 * after a restore it opens the workspace. The control: a session signed out everywhere still
 * reloads into the log-in page.
 */
import type { BrowserContext, Page } from "@playwright/test";

import { API_ORIGIN } from "../support/env";
import {
  api,
  label,
  logInThroughTheApp,
  REFRESH_COOKIES,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
} from "../support/fe2";
import { memberOf, recordRequests, restore, suspend, SUSPEND_BOUND_MS } from "../support/fe3";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 180_000 });

const LOCALE = "en" as const;

async function signIn(page: Page, context: BrowserContext, email: string): Promise<void> {
  await useAddress(context);
  await useLocale(context, LOCALE);
  await logInThroughTheApp(page, LOCALE, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
}

async function hasRefreshCookie(context: BrowserContext): Promise<boolean> {
  return (await context.cookies(WEB)).some((cookie) => REFRESH_COOKIES.includes(cookie.name));
}

async function reloadIntoSuspended(page: Page, context: BrowserContext): Promise<string[]> {
  const seen = recordRequests(context);
  await page.reload();
  const state = page.locator('[data-session="workspace-suspended"]');
  await expect(state).toBeVisible({ timeout: SUSPEND_BOUND_MS });
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    label(LOCALE, "app.gates.suspended.title"),
  );
  expect(page.url()).not.toContain("/login");
  expect(await hasRefreshCookie(context)).toBe(true);
  return seen;
}

test("a reload of a suspended workspace keeps the owner signed in; Check again; restore", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("be1s");
  await signIn(page, context, owner.email);
  await suspend(owner.slug);

  const seen = await reloadIntoSuspended(page, context);
  // Nothing but the refresh: no API call from the page and no stream ticket.
  expect(seen.filter((line) => line.includes(`${API_ORIGIN}/api/`))).toEqual([]);
  expect(seen.filter((line) => line.includes("/session/refresh")).length).toBeGreaterThan(0);

  // Still suspended: Check again asks again and stays - no replay, no sign-out.
  const checkAgain = page.getByRole("button", {
    name: label(LOCALE, "app.gates.suspended.checkAgain"),
  });
  const asked = page.waitForResponse((r) => r.url().endsWith("/session/refresh"));
  await checkAgain.click();
  expect((await asked).status()).toBe(403);
  await expect(page.locator('[data-session="workspace-suspended"]')).toBeVisible();
  expect(await hasRefreshCookie(context)).toBe(true);

  await restore(owner.slug);
  await checkAgain.click();
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
  await expect.poll(() => seen.some((line) => line.includes("/events/ticket"))).toBe(true);
});

test("a member's reload of a suspended workspace keeps them signed in too", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("be1m");
  const mate = await memberOf(owner);
  await signIn(page, context, mate.email);
  await suspend(owner.slug);
  await reloadIntoSuspended(page, context);
  await page.getByRole("button", { name: label(LOCALE, "app.account.signOut") }).click();
  await page.waitForURL(/\/login/);
  expect(await hasRefreshCookie(context)).toBe(false);
});

test("control: a session signed out everywhere still reloads into the log-in page", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("be1c");
  await signIn(page, context, owner.email);
  const ended = await api("POST", "/api/v1/auth/logout-all", { token: owner.token });
  expect([200, 204]).toContain(ended.status);
  await page.reload();
  await page.waitForURL(/\/login/, { timeout: 30_000 });
  expect(await hasRefreshCookie(context)).toBe(false);
});
