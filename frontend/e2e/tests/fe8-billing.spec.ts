/**
 * FE-8's journeys (BJ-02 .. BJ-11, ADR-202) against FE-0's profile and the fake Paymob page:
 *
 * - J5, the plan's acceptance: an owner upgrades to pro, pays on the fake page, comes back signed
 *   in, and "Payment received" appears only after the payment reads succeeded - then the plan is
 *   pro through the API.
 * - the return page against a DECLINED payment whose redirect still says success=true (failed is
 *   shown, success never), against a payment with NO callback (confirming, never success) and
 *   against a callback that arrives LATE (success only after it).
 * - the roles: an administrator reads the plan with no action and never asks for the owner's
 *   billing reads; a member has no Billing.
 * - FEB-42's role change on /team, FEB-44's sender on /team/invitations, FEB-45's modes on the
 *   account page.
 *
 * Every wait is on a fact - an answer, a state the page reports, a read-back through the API -
 * never on a press having happened or a fixed moment (BJ-01). The return page's states are
 * recorded on every change by a MutationObserver installed before the page loads, so "success was
 * never shown" is a fact about every frame the page drew, not about the last one.
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import { API_ORIGIN } from "../support/env";
import {
  api,
  logInThroughTheApp,
  nothingReachable,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
  type Account,
} from "../support/fe2";
import { login, memberOf, revoke } from "../support/fe3";
import { freshEmail } from "../support/helpers";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 240_000 });

const BOUND = 30_000;
const FAKE_PAYMOB = "http://localhost:8026/paymob/";

async function axe(page: Page, state: string): Promise<void> {
  await expect(page.locator('[role="dialog"][data-state="closed"]')).toHaveCount(0);
  await expect(page.locator('button[aria-busy="true"]')).toHaveCount(0);
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every(
        (animation) =>
          animation.playState !== "running" ||
          animation.effect?.getComputedTiming().iterations === Infinity,
      ),
  );
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"])
    .analyze();
  expect(
    result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => `${state}: ${v.id} | ${v.nodes.map((n) => n.target.join(" ")).join(" || ")}`),
  ).toEqual([]);
}

async function signIn(page: Page, context: BrowserContext, email: string): Promise<void> {
  await useAddress(context);
  await useLocale(context, "en");
  await logInThroughTheApp(page, "en", email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: BOUND });
}

/** Every state the return page draws, in order, kept on window across its whole life. */
async function recordReturnStates(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    const seen: string[] = [];
    (window as unknown as { __fe8States: string[] }).__fe8States = seen;
    const note = () => {
      const node = document.querySelector("[data-return-state]");
      const state = node?.getAttribute("data-return-state");
      if (state && seen[seen.length - 1] !== state) seen.push(state);
      // The panel's own title: the page's script data carries every message, this phrase too.
      const title = document.querySelector("[data-payment-title]")?.textContent ?? "";
      if (title.includes("Payment received")) {
        if (seen[seen.length - 1] !== "text:received") seen.push("text:received");
      }
    };
    new MutationObserver(note).observe(document, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  });
}

async function statesOf(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __fe8States?: string[] }).__fe8States ?? []);
}

/** Sends the fake page the journey's outcome (the API never sees it - EC8-04). */
async function payWith(context: BrowserContext, outcome: string): Promise<void> {
  await context.route(`${FAKE_PAYMOB}**`, async (route) => {
    const url = new URL(route.request().url());
    for (const [key, value] of new URLSearchParams(outcome)) url.searchParams.set(key, value);
    await route.continue({ url: url.toString() });
  });
}

async function startProCheckout(page: Page): Promise<void> {
  await page.goto(`${WEB}/billing`);
  const upgrade = page.locator('[data-plan-price="pro:monthly"]');
  await expect(upgrade).toBeVisible({ timeout: BOUND });
  const started = page.waitForResponse(
    (response) =>
      response.url().startsWith(`${API_ORIGIN}/api/v1/billing/checkout`) &&
      response.request().method() === "POST",
    { timeout: BOUND },
  );
  await upgrade.click();
  expect((await started).status()).toBe(201);
  await page.waitForURL(`${WEB}/billing/checkout/return**`, { timeout: BOUND });
}

async function planCode(owner: Account): Promise<string> {
  const token = await login(owner.email, owner.password, owner.slug);
  const answer = await api("GET", "/api/v1/billing/subscription", { token });
  return (answer.json as { subscription: { plan: { code: string } } | null }).subscription?.plan
    .code as string;
}

test.describe("FE-8 billing (BJ)", () => {
  test("BJ-02 J5: upgrade, pay, come back signed in, success only after the API says so", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("bj5");
    await recordReturnStates(context);
    await signIn(page, context, owner.email);
    await startProCheckout(page);
    // The query string is dropped unread on arrival (RT-03).
    await expect(page).toHaveURL(`${WEB}/billing/checkout/return`, { timeout: BOUND });
    await expect(page.locator('[data-return-state="succeeded"]')).toBeVisible({ timeout: BOUND });
    const states = await statesOf(page);
    const firstSuccess = states.indexOf("succeeded");
    expect(firstSuccess).toBeGreaterThan(0);
    expect(states.slice(0, firstSuccess)).not.toContain("text:received");
    expect(await planCode(owner)).toBe("pro");
    await nothingReachable(page, context);
    await axe(page, "return succeeded");
    await page.getByRole("link", { name: "Go to Billing" }).click();
    await expect(page.locator('[data-current-plan="pro"]')).toBeVisible({ timeout: BOUND });
    await axe(page, "plan after payment");
  });

  test("BJ-03: a declined payment whose redirect says success=true is shown as failed", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("bj3");
    await recordReturnStates(context);
    await payWith(context, "outcome=declined");
    await signIn(page, context, owner.email);
    await startProCheckout(page);
    // BILL-07: a hosted decline leaves the payment pending with its reason - shown as declined.
    await expect(page.locator('[data-return-state="declined"]')).toBeVisible({ timeout: BOUND });
    await expect(page.getByText("The payment did not go through")).toBeVisible();
    const states = await statesOf(page);
    expect(states).not.toContain("succeeded");
    expect(states).not.toContain("text:received");
    expect(await planCode(owner)).not.toBe("pro");
    await axe(page, "return declined");
  });

  test("BJ-04: no callback yet - confirming, never success", async ({ page, context }) => {
    const owner = await verifiedOwner("bj4");
    await recordReturnStates(context);
    await payWith(context, "outcome=pending");
    await signIn(page, context, owner.email);
    // Three answered reads of the payment, each still pending: a fact, not a moment.
    let reads = 0;
    page.on("response", (response) => {
      if (response.url().includes("/api/v1/billing/payments/")) reads += 1;
    });
    await startProCheckout(page);
    await expect(page.locator('[data-return-state="confirming"]')).toBeVisible({ timeout: BOUND });
    await expect.poll(() => reads, { timeout: BOUND }).toBeGreaterThanOrEqual(3);
    await expect(page.locator('[data-return-state="confirming"]')).toBeVisible();
    expect(await statesOf(page)).not.toContain("succeeded");
    await axe(page, "return confirming");
  });

  test("BJ-05: a late callback - success shown only after it arrives", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("bj5l");
    await recordReturnStates(context);
    await payWith(context, "outcome=late&delay=6");
    await signIn(page, context, owner.email);
    await startProCheckout(page);
    await expect(page.locator('[data-return-state="succeeded"]')).toBeVisible({ timeout: BOUND });
    const states = await statesOf(page);
    expect(states.indexOf("confirming")).toBeGreaterThanOrEqual(0);
    expect(states.indexOf("confirming")).toBeLessThan(states.indexOf("succeeded"));
    expect(await planCode(owner)).toBe("pro");
  });

  test("BJ-09: an administrator reads the plan and never asks for the owner's billing", async ({
    page,
    context,
  }) => {
    const owner = await verifiedOwner("bj9");
    const admin = await memberOf(owner, "tenant_admin");
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.url().startsWith(API_ORIGIN)) requests.push(new URL(request.url()).pathname);
    });
    await signIn(page, context, admin.email);
    // On a phone the navigation is a closed sheet: the page is opened by its address.
    await page.goto(`${WEB}/billing`);
    await expect(page.locator("[data-current-plan]")).toBeVisible({ timeout: BOUND });
    await expect(page.locator('[data-action^="plan-"]')).toHaveCount(0);
    await expect(
      page.getByText("Only the workspace owner can change the plan or pay."),
    ).toBeVisible();
    await page.goto(`${WEB}/billing/invoices`);
    await expect(
      page.getByText("Only the workspace owner can change the plan or pay."),
    ).toBeVisible({
      timeout: BOUND,
    });
    for (const owned of [
      "/api/v1/billing/summary",
      "/api/v1/billing/custom-offers",
      "/api/v1/invoices",
    ]) {
      expect(requests.filter((path) => path.startsWith(owned))).toEqual([]);
    }
    await axe(page, "admin billing");
  });

  test("BJ-09: a member has no Billing", async ({ page, context }) => {
    const owner = await verifiedOwner("bj9m");
    const member = await memberOf(owner, "member");
    await signIn(page, context, member.email);
    await expect(page.getByRole("link", { name: "Billing", exact: true })).toHaveCount(0);
  });

  test("BJ-10: FEB-42 change role, FEB-44 sender, FEB-45 modes", async ({ page, context }) => {
    const owner = await verifiedOwner("bj10");
    const member = await memberOf(owner, "member");
    await signIn(page, context, owner.email);

    await page.goto(`${WEB}/team`);
    const change = page.locator(`[data-change-role="${member.userId}"]`);
    await expect(change).toBeVisible({ timeout: BOUND });
    await change.click();
    await page.getByRole("radio", { name: /Administrator/ }).check();
    const saved = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/workspace/members/${member.userId}`) &&
        response.request().method() === "PATCH",
    );
    await page.locator('[data-action="confirm"]').click();
    expect((await saved).status()).toBe(200);
    const roster = await api("GET", "/api/v1/workspace/members", { token: owner.token });
    const row = (roster.json as { members: Array<{ user_id: string; role: string }> }).members.find(
      (m) => m.user_id === member.userId,
    );
    expect(row?.role).toBe("tenant_admin");
    await axe(page, "team after role change");

    // The plan's seats are the owner's and this member's: free one for the invitation below.
    await revoke(owner, member);

    const invited = await api("POST", "/api/v1/invitations", {
      token: owner.token,
      body: { email: freshEmail("bj10inv") },
    });
    expect(invited.status).toBe(201);
    await page.goto(`${WEB}/team/invitations`);
    await expect(page.locator("[data-invitation-sender]").first()).toContainText("Invited by", {
      timeout: BOUND,
    });
    await expect(page.locator("[data-invitation-sent]").first()).toContainText("Sent");

    await page.goto(`${WEB}/settings/account`);
    await expect(page.locator('[data-action="save-password"]')).toHaveText("Change password", {
      timeout: BOUND,
    });
    await expect(page.locator('[data-action="password-mode"]')).toHaveCount(0);
    await expect(page.locator('[data-action="disconnect-google"]')).toHaveCount(0);
  });
});
