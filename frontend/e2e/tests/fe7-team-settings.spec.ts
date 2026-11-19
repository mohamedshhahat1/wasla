/**
 * FE-7's journeys (TJ-02 .. TJ-10, ADR-201) against FE-0's profile: the plan's acceptance through
 * the screens - J3 (invite → the e-mail → accept → the member's limited view → removed → told at
 * once, never sent to /verify), the ownership transfer, closing an account with the fake Google's
 * re-authentication - and the workspace settings, closing a workspace by its typed address, the
 * last-used workspace at sign-in (FEB-36) and the account's language reaching its e-mails (FEB-09).
 * Every step waits on a fact - an answer with its status, a row, a read-back, an e-mail in the
 * mailbox - never on a press having happened or a fixed moment (TJ-01).
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page, Response } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import {
  api,
  label,
  logInThroughTheApp,
  PASSWORD,
  testAddress,
  useAddress,
  useLocale,
  verifiedOwner,
  waitForCode,
  waitForLink,
  WEB,
  type Account,
} from "../support/fe2";
import { login, memberOf, recordRequests, REVOKE_BOUND_MS, suspend } from "../support/fe3";
import { call } from "../support/fe6";
import { freshEmail, mailbox } from "../support/helpers";
import { actorContext, expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 300_000 });

const BOUND = 30_000;
const TEAM = "app.team";
const SETTINGS = "app.settings";
const ACCOUNT = "app.accountPage";

async function axe(page: Page, state: string): Promise<void> {
  await expect(page.getByRole("menu")).toHaveCount(0);
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
  test.info().annotations.push({
    type: `axe ${state}`,
    description: JSON.stringify(result.violations.map((v) => [v.id, v.impact, v.nodes.length])),
  });
  expect(
    result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => `${state}: ${v.id} | ${v.nodes.map((n) => n.target.join(" ")).join(" || ")}`),
  ).toEqual([]);
}

async function signIn(page: Page, context: BrowserContext, locale: Locale, email: string) {
  await useAddress(context);
  await useLocale(context, locale);
  await logInThroughTheApp(page, locale, email);
  await page.waitForURL(`${WEB}/`);
  await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: BOUND });
}

/** The answer of the API call an action sends: the fact a step waits on. */
function answerOf(page: Page, method: string, path: RegExp): Promise<Response> {
  return page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        url.origin === API_ORIGIN &&
        response.request().method() === method &&
        path.test(url.pathname)
      );
    },
    { timeout: BOUND },
  );
}

/** Nothing typed or secret in the address bar, the history or the tab's storage (TJ-08). */
async function nothingKept(page: Page, secrets: readonly string[]): Promise<void> {
  const kept = await page.evaluate(() =>
    JSON.stringify({
      url: window.location.href,
      history: window.history.state as unknown,
      local: { ...window.localStorage },
      session: { ...window.sessionStorage },
    }),
  );
  for (const secret of secrets) expect(kept).not.toContain(secret);
}

async function newWorkspace(owner: Account, slug: string): Promise<void> {
  const made = await call(owner, "POST", "/api/v1/workspaces", {
    name: `Workspace ${slug}`,
    slug,
    timezone: "Africa/Cairo",
  });
  expect(made.status).toBe(201);
}

async function switchTo(owner: Account, slug: string): Promise<void> {
  const answer = await api("POST", "/api/v1/auth/workspace", {
    token: owner.token,
    body: { workspace_slug: slug },
  });
  expect(answer.status).toBe(200);
}

function slug(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Accepts an e-mailed invitation and verifies the address, as FE-3's memberOf does. */
async function acceptAndVerify(email: string, workspaceSlug: string): Promise<void> {
  const link = await waitForLink(email, "/invitations/accept");
  const token = new URL(link).searchParams.get("token");
  const accepted = await api("POST", "/api/v1/invitations/accept", {
    address: testAddress(),
    body: { token, password: PASSWORD, full_name: "E2E Invitee" },
  });
  expect(accepted.status).toBe(200);
  const bearer = await login(email, PASSWORD, workspaceSlug);
  const tried = new Set<string>();
  let verified = 0;
  for (let attempt = 0; attempt < 3 && verified !== 200; attempt += 1) {
    await api("POST", "/api/v1/auth/email/verification/send", { token: bearer });
    const code = await waitForCode(email, tried);
    tried.add(code);
    verified = (
      await api("POST", "/api/v1/auth/email/verification/verify", { token: bearer, body: { code } })
    ).status;
  }
  expect(verified).toBe(200);
}

test("TJ-02: J3 through the screens - invited, limited, removed, told at once", async ({
  page,
  context,
  browser,
}) => {
  const owner = await verifiedOwner("tj2");
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/team/invitations`);
  await expect(page.locator('[data-screen="invitations"]')).toBeVisible({ timeout: BOUND });
  await axe(page, "invitations (owner, en)");

  const email = freshEmail("j3");
  const field = page.locator('[data-field="invite-email"]');
  await expect(field).toBeEnabled();
  await field.fill(email);
  const sent = answerOf(page, "POST", /^\/api\/v1\/invitations$/);
  await page.locator('[data-action="invite"]').click();
  expect((await sent).status()).toBe(201);
  await expect(page.locator("[data-invitations-list] li", { hasText: email })).toBeVisible({
    timeout: BOUND,
  });
  await nothingKept(page, [email]);

  // The e-mail arrived, in English (the inviter's language).
  await expect
    .poll(async () => (await mailbox.messagesTo(email)).length, { timeout: BOUND })
    .toBeGreaterThan(0);
  await acceptAndVerify(email, owner.slug);

  const memberContext = await actorContext(browser);
  const memberPage = await memberContext.newPage();
  const requests = recordRequests(memberContext);
  await signIn(memberPage, memberContext, "en", email);
  if (!test.info().project.name.includes("phone")) {
    // On a phone the navigation sits behind the menu button; FE-3's JR-02 covers it there.
    const nav = memberPage.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: label("en", "app.nav.team") })).toBeVisible();
    await expect(nav.getByRole("link", { name: label("en", "app.nav.settings") })).toHaveCount(0);
  }
  await memberPage.goto(`${WEB}/team`);
  await expect(memberPage.locator("[data-members-list] li")).toHaveCount(2, { timeout: BOUND });
  for (const action of ["remove", "make-owner", "reinstate"]) {
    await expect(memberPage.locator(`[data-action="${action}"]`)).toHaveCount(0);
  }
  await expect(memberPage.locator('[data-action="leave"]')).toHaveCount(1);
  await memberPage.goto(`${WEB}/team/invitations`);
  await expect(memberPage.getByText(label("en", `${TEAM}.adminsOnly`))).toBeVisible();
  expect(requests.filter((url) => url.includes("/api/v1/invitations"))).toEqual([]);
  expect(requests.filter((url) => url.includes("/api/v1/audit-logs"))).toEqual([]);
  await memberPage.goto(`${WEB}/team`);
  await expect(memberPage.locator("[data-members-list] li")).toHaveCount(2, { timeout: BOUND });
  await axe(memberPage, "team (member, en)");

  // The owner removes them from /team.
  await page.goto(`${WEB}/team`);
  const row = page.locator("[data-member-row]", { hasText: email });
  await row.locator('[data-action="remove"]').click();
  const removed = answerOf(page, "DELETE", /^\/api\/v1\/workspace\/members\//);
  await page.getByRole("dialog").locator('[data-action="confirm"]').click();
  expect((await removed).status()).toBe(200);
  await expect(page.locator("[data-member-row]", { hasText: email })).toHaveCount(0, {
    timeout: BOUND,
  });

  // The member is told at once (the stream closes), and a reload never lands on /verify (FL-05).
  await expect(memberPage.locator('[data-gate="revoked"]')).toBeVisible({
    timeout: REVOKE_BOUND_MS,
  });
  await expect(memberPage.getByText(label("en", "app.gates.revoked.title"))).toBeVisible();
  await memberPage.reload();
  await memberPage.waitForURL((url) => !url.pathname.startsWith("/team"), { timeout: BOUND });
  expect(new URL(memberPage.url()).pathname).not.toBe("/verify");
  await nothingKept(memberPage, [PASSWORD]);
  await memberContext.close();
});

test("TJ-03: the owner hands over ownership and becomes an administrator at once", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("tj3");
  const admin = await memberOf(owner, "tenant_admin");
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/team`);
  const row = page.locator("[data-member-row]", { hasText: admin.email });
  await row.locator('[data-action="make-owner"]').click();
  await expect(page.getByRole("dialog")).toContainText(label("en", `${TEAM}.ownerBody`));
  const handed = answerOf(page, "POST", /^\/api\/v1\/workspace\/ownership$/);
  await page.getByRole("dialog").locator('[data-action="confirm"]').click();
  expect((await handed).status()).toBe(200);
  // The caller's own controls change at once: no Make owner, no Close workspace.
  await expect(page.locator('[data-action="make-owner"]')).toHaveCount(0, { timeout: BOUND });
  await page.goto(`${WEB}/settings/workspace`);
  await expect(page.locator("[data-settings-nav]")).toBeVisible({ timeout: BOUND });
  await expect(page.getByRole("link", { name: label("en", `${SETTINGS}.nav.close`) })).toHaveCount(
    0,
  );
  const members = await call(admin, "GET", "/api/v1/workspace/members");
  const roles = (members.json as { members: Array<{ email: string; role: string }> }).members;
  expect(roles.find((m) => m.email === admin.email)?.role).toBe("tenant_owner");
  expect(roles.find((m) => m.email === owner.email)?.role).toBe("tenant_admin");
});

test("TJ-04: an account closes with the fake Google's re-authentication; an owner is told what to hand over", async ({
  page,
  context,
  browser,
}) => {
  // An owner first: refused with the workspace named, nothing closed.
  const owner = await verifiedOwner("tj4");
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/settings/account`);
  await page.locator('[data-action="close-account"]').click();
  const dialog = page.getByRole("dialog");
  await dialog.locator('[data-field="close-password"]').fill(PASSWORD);
  const refused = answerOf(page, "DELETE", /^\/api\/v1\/auth\/me$/);
  await dialog.locator('[data-action="confirm-close-account"]').click();
  expect((await refused).status()).toBe(409);
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-owned-workspaces]")).toContainText(`Workspace ${owner.slug}`);
  await expect(page.locator('[data-action="hand-over"]')).toHaveCount(1);
  await nothingKept(page, [PASSWORD]);

  // A member with no workspace of their own connects Google, then closes with Google's proof.
  const member = await memberOf(owner);
  const memberContext = await actorContext(browser);
  const memberPage = await memberContext.newPage();
  // The address route first, then the Google routes: a route registered later is tried first,
  // so these must come after every other route this context gets.
  const address = await useAddress(memberContext);
  await useLocale(memberContext, "en");
  for (const flow of ["link", "reauth"]) {
    await memberContext.route(`${WEB}/session/google/${flow}/authorize`, async (route) => {
      const headers = { ...route.request().headers(), "x-wasla-e2e-client-ip": address };
      const response = await route.fetch({ headers });
      const body = (await response.json()) as { authorization_url: string };
      await route.fulfill({
        response,
        json: {
          authorization_url: `${body.authorization_url}&email=${encodeURIComponent(member.email)}`,
        },
      });
    });
  }
  await logInThroughTheApp(memberPage, "en", member.email);
  await memberPage.waitForURL(`${WEB}/`);
  await expect(memberPage.locator('[data-page="home"]')).toBeVisible({ timeout: BOUND });
  await memberPage.goto(`${WEB}/settings/account`);
  await memberPage.locator('[data-action="connect-google"]').click();
  await expect(memberPage.locator('[data-google-status="connected"]')).toBeVisible({
    timeout: BOUND,
  });
  const identities = await call(member, "GET", "/api/v1/auth/me");
  test
    .info()
    .annotations.push({ type: "me after link", description: JSON.stringify(identities.status) });

  await memberPage.locator('[data-action="close-account"]').click();
  const closeDialog = memberPage.getByRole("dialog");
  await closeDialog.getByRole("radio", { name: label("en", `${ACCOUNT}.close.byGoogle`) }).check();
  await closeDialog.locator('[data-action="confirm-close-account"]').click();
  await memberPage.waitForURL(/\/settings\/account/, { timeout: BOUND });
  await expect(memberPage.locator('[data-reauth="ready"]')).toBeVisible({ timeout: BOUND });
  expect(memberPage.url()).not.toMatch(/[?&](code|state|reauth)=/);
  await nothingKept(memberPage, ["e2e-code-"]);
  const closed = memberPage.waitForResponse(`${WEB}/session/account/close`);
  await memberPage.getByRole("dialog").locator('[data-action="confirm-close-account"]').click();
  expect((await closed).status()).toBe(200);
  await memberPage.waitForURL(/\/login/, { timeout: BOUND });
  await expect(memberPage.getByText(label("en", "auth.login.notes.accountClosed"))).toBeVisible();
  const again = await api("POST", "/api/v1/auth/login", {
    body: { email: member.email, password: PASSWORD },
  });
  expect(again.status).not.toBe(200);
  await memberContext.close();
});

test("TJ-05: workspace settings save only what changed; closing needs the address typed", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("tj5");
  await newWorkspace(owner, slug("next"));
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/settings/workspace`);
  const name = page.locator('[data-field="workspace-name"]');
  await expect(name).toBeEnabled({ timeout: BOUND });
  // Clean: Save says it has nothing to do; a change brings Discard and "unsaved" (SW-03).
  const save = page.locator('[data-action="save-workspace"]');
  await expect(save).toHaveAttribute("aria-disabled", "true");
  await expect(page.locator('[data-action="discard-workspace"]')).toHaveCount(0);
  await name.fill(`Renamed ${owner.slug}`);
  await expect(save).not.toHaveAttribute("aria-disabled", "true");
  await expect(page.locator('[data-action="discard-workspace"]')).toBeVisible();
  const saved = answerOf(page, "PATCH", /^\/api\/v1\/workspace$/);
  await page.locator('[data-action="save-workspace"]').click();
  const answer = await saved;
  expect(answer.status()).toBe(200);
  expect(Object.keys(JSON.parse(answer.request().postData() ?? "{}") as object)).toEqual(["name"]);
  await expect(page.locator("[data-local-time]")).toBeVisible();
  await axe(page, "workspace settings (owner, en)");

  await page.goto(`${WEB}/settings/activity`);
  await expect(page.locator("[data-activity-list] li").first()).toBeVisible({ timeout: BOUND });
  // A rename leaves no trail by design (WorkspaceService: "Auditing a rename would add rows
  // nobody filters on"); the new workspace's subscription does, in words.
  await expect(page.locator("[data-activity-list]")).toContainText(
    label("en", `${SETTINGS}.activity.actions.subscription_started`),
  );
  await axe(page, "activity (owner, en)");

  await page.goto(`${WEB}/settings/workspace/close`);
  const typed = page.locator('[data-field="close-confirmation"]');
  await typed.fill(`${owner.slug}x`);
  await expect(page.locator('[data-action="close-workspace"]')).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await typed.fill(owner.slug);
  const closing = answerOf(page, "DELETE", /^\/api\/v1\/workspace$/);
  await page.locator('[data-action="close-workspace"]').click();
  expect((await closing).status()).toBe(200);
  await page.waitForURL((url) => !url.pathname.startsWith("/settings"), { timeout: BOUND });
  await nothingKept(page, [owner.slug]);
});

test("TJ-06: a sign-in opens the last-used workspace, never a suspended one (FEB-36)", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("tj6");
  const b = slug("bbb");
  const c = slug("ccc");
  await newWorkspace(owner, b);
  await newWorkspace(owner, c);
  await switchTo(owner, c);
  await signIn(page, context, "en", owner.email);
  await expect(page.locator("[data-app-bar]")).toContainText(`Workspace ${c}`);

  await suspend(c);
  await context.clearCookies();
  await signIn(page, context, "en", owner.email);
  await expect(page.locator("[data-app-bar]")).toContainText(`Workspace ${owner.slug}`);
});

test("TJ-07: the account's language reaches the page and its e-mails (FEB-09)", async ({
  page,
  context,
}) => {
  const owner = await verifiedOwner("tj7");
  await signIn(page, context, "en", owner.email);
  await page.goto(`${WEB}/settings/account`);
  const saved = answerOf(page, "PATCH", /^\/api\/v1\/auth\/me$/);
  await page.getByRole("radio", { name: "العربية" }).check();
  await page.locator('[data-action="save-language"]').click();
  expect((await saved).status()).toBe(200);
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl", { timeout: BOUND });
  await axe(page, "account (ar)");

  const before = (await mailbox.messagesTo(owner.email)).length;
  const reset = await api("POST", "/api/v1/auth/password-reset/request", {
    body: { email: owner.email },
  });
  expect([200, 202]).toContain(reset.status);
  await expect
    .poll(async () => (await mailbox.messagesTo(owner.email)).length, { timeout: BOUND })
    .toBeGreaterThan(before);
  const [latest] = await mailbox.messagesTo(owner.email);
  expect(latest!.html).toContain('dir="rtl"');
});
