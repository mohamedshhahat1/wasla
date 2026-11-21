/**
 * KY-02: one keyboard-only journey (FE-5's CJ-12 half that was not written) over FE-4's inbox,
 * FE-5's customers and leads and FE-6's AI and knowledge screens; FE-8 leftovers (KY8-02) add the
 * billing journey. No pointer at all: the first test reads this file and fails if a mouse call
 * appears in it. Each step waits on a fact.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { label, logInThroughTheApp, useAddress, useLocale, WEB } from "../support/fe2";
import { memberOf } from "../support/fe3";
import { connectedOwner, freshCustomer, inbound, text } from "../support/fe4";
import { checkoutAnswer, printCalls, returnSucceeded, stubPrint } from "../support/fe8";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });
test.describe.configure({ timeout: 300_000 });

const KEYS_ONLY =
  /\.(click|dblclick|hover|tap|dragTo|check|uncheck|selectOption|setChecked)\(|page\.mouse|\.mouse\./;

test("KY-02: this journey uses no pointer API", () => {
  const source = readFileSync(fileURLToPath(import.meta.url), "utf-8")
    .split("\n")
    .filter((line) => !line.includes("KEYS_ONLY"))
    .join("\n");
  expect(source.match(KEYS_ONLY)).toBeNull();
});

type Target =
  | { name: string }
  | { action: string }
  | { role: string }
  | { inside: string }
  | { rowLinkPrefix: string }
  | { field: string }
  | { planPrice: string };

/** Tab (or Shift+Tab) until the focused element is the target, bounded; the fact is focus. */
async function tabTo(page: Page, target: Target, reverse = false): Promise<void> {
  for (let stop = 0; stop < 200; stop += 1) {
    const hit = await page.evaluate((wanted) => {
      const el = document.activeElement;
      if (!el) return false;
      if ("name" in wanted) return (el.textContent ?? "").trim() === wanted.name;
      if ("action" in wanted) return el.getAttribute("data-action") === wanted.action;
      if ("role" in wanted) return el.getAttribute("role") === wanted.role;
      if ("inside" in wanted) return el.closest(wanted.inside) !== null;
      if ("field" in wanted) return el.getAttribute("data-field") === wanted.field;
      if ("planPrice" in wanted) return el.getAttribute("data-plan-price") === wanted.planPrice;
      return (el.getAttribute("data-row-link") ?? "").startsWith(wanted.rowLinkPrefix);
    }, target);
    if (hit) return;
    await page.keyboard.press(reverse ? "Shift+Tab" : "Tab");
  }
  throw new Error(`never reached by Tab: ${JSON.stringify(target)}`);
}

for (const locale of ["en", "ar"] as const) {
  test(`KY-02 (${locale}): inbox → customer → lead → agent → knowledge, keyboard only`, async ({
    page,
    context,
  }) => {
    const line = await connectedOwner(`kb${locale}`);
    await inbound(line, freshCustomer(), text("Hello"));
    await useAddress(context);
    await useLocale(context, locale);
    await logInThroughTheApp(page, locale, line.owner.email);
    await page.waitForURL(`${WEB}/`);
    await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });

    // Home → the warning's link → the agents list (Enter on a link).
    await tabTo(page, { name: label(locale, "app.homeWarnings.noAgentAction") });
    await page.keyboard.press("Enter");
    await page.waitForURL(`${WEB}/ai/agents`);

    // New agent: the dialog keeps focus; Escape returns it to the opener.
    const create = label(locale, "app.ai.emptyAction");
    await tabTo(page, { name: create });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => (document.activeElement?.textContent ?? "").trim()))
      .toBe(create);
    await page.keyboard.press("Enter");
    // A dialog focuses its first control (the close button); Tab moves on to the field.
    await tabTo(page, { field: "agent-name" });
    await page.keyboard.type("Keyboard agent");
    await page.keyboard.press("Tab");
    await page.keyboard.type("You answer questions about the test company.");
    const created = page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/api\/v1\/agents$/.test(r.url()),
    );
    // Enter in a text field submits its form (implicit submission): keys only.
    await tabTo(page, { field: "agent-name" }, true);
    await page.keyboard.press("Enter");
    expect((await created).status()).toBe(201);
    await page.waitForURL(/\/ai\/agents\/[0-9a-f-]{36}$/);
    // The dialog's field is gone: the page's heading holds the focus (a fact, before any Tab).
    await expect(page.locator("#agent-title")).toBeFocused();

    // Activate, then make default - Enter on each; the status says so, and focus lands on the
    // next state's first action, never nowhere.
    await tabTo(page, { action: "activate" });
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-action="makeDefault"]')).toBeFocused();
    await tabTo(page, { action: "makeDefault" }, true);
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-status-sentence="answering"]')).toBeVisible();
    await expect(page.locator("[data-agent-status] [data-action]").first()).toBeFocused();

    // A tool's switch: Space.
    await tabTo(page, { role: "switch" }, true);
    const before = await page.evaluate(() => document.activeElement?.getAttribute("aria-checked"));
    const toggled = page.waitForResponse(
      (r) => /\/tools(\/|$)/.test(new URL(r.url()).pathname) && r.request().method() !== "GET",
    );
    await page.keyboard.press("Space");
    await toggled;
    await expect
      .poll(() => page.locator('[role="switch"]').first().getAttribute("aria-checked"))
      .not.toBe(before);

    // Knowledge: a new base, "Add text", then the row opens its panel; Escape closes it.
    await page.goto(`${WEB}/knowledge`);
    await tabTo(page, { action: "new-base" });
    await page.keyboard.press("Enter");
    // A dialog focuses its first control (the close button); Tab moves on to the field.
    await tabTo(page, { field: "base-name" });
    await page.keyboard.type("Keys");
    await page.keyboard.press("Enter");
    await page.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/);
    await expect(page.locator("#base-title")).toBeFocused();
    await tabTo(page, { action: "add-text" });
    await page.keyboard.press("Enter");
    // A dialog focuses its first control (the close button); Tab moves on to the field.
    await tabTo(page, { field: "text-title" });
    await page.keyboard.type("Opening hours");
    await page.keyboard.press("Tab");
    await page.keyboard.type("Open from 9 to 5. (e2e)");
    const added = page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/documents$/.test(new URL(r.url()).pathname),
    );
    await tabTo(page, { field: "text-title" }, true);
    await page.keyboard.press("Enter");
    expect((await added).status()).toBe(202);
    await tabTo(page, { inside: "[data-document-row]" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/doc=/);
    await page.keyboard.press("Escape");
    await expect(page).not.toHaveURL(/doc=/);

    // The inbox → a conversation → the customer's page → the lead's status, keys only.
    await page.goto(`${WEB}/inbox`);
    await tabTo(page, { inside: "[data-conversation-row]" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/inbox\/[0-9a-f-]{36}/);
    await page.goto(`${WEB}/contacts`);
    await tabTo(page, { rowLinkPrefix: "/contacts/" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/contacts\/[0-9a-f-]{36}$/);
    // Back by the page's own link: a browser shortcut never reaches a headless page.
    await tabTo(page, { name: label(locale, "app.customers.contact.back") });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(`${WEB}/contacts`);

    // FE-7 (KY7-02): Team → invitations → invite with Enter → revoke behind
    // its dialog (Escape returns focus to the button, then confirm).
    // (Team itself sits behind the phone's menu button: opened by address, as the inbox is above.)
    await page.goto(`${WEB}/team`);
    await expect(page.locator("[data-members-list]")).toBeVisible({ timeout: 30_000 });
    await tabTo(page, { name: label(locale, "app.team.tabs.invitations") });
    await page.keyboard.press("Enter");
    await page.waitForURL(`${WEB}/team/invitations`);
    await tabTo(page, { field: "invite-email" });
    await page.keyboard.type(`kb.${locale}.${Date.now().toString(36)}@example.com`);
    const invited = page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/api\/v1\/invitations$/.test(r.url()),
    );
    await page.keyboard.press("Enter");
    expect((await invited).status()).toBe(201);
    await tabTo(page, { action: "revoke" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator('[data-action="revoke"]').first()).toBeFocused();
    await page.keyboard.press("Enter");
    await tabTo(page, { action: "confirm" });
    const revoked = page.waitForResponse(
      (r) => r.request().method() === "DELETE" && /\/api\/v1\/invitations\//.test(r.url()),
    );
    await page.keyboard.press("Enter");
    expect((await revoked).status()).toBe(200);

    // Settings: the name changes, the save bar appears, Save with Enter; focus is never lost.
    await page.goto(`${WEB}/settings/workspace`);
    await tabTo(page, { field: "workspace-name" });
    await page.keyboard.press("End");
    await page.keyboard.type(" K");
    const patched = page.waitForResponse(
      (r) => r.request().method() === "PATCH" && /\/api\/v1\/workspace$/.test(r.url()),
    );
    await tabTo(page, { action: "save-workspace" });
    await page.keyboard.press("Enter");
    expect((await patched).status()).toBe(200);
    await expect
      .poll(() => page.evaluate(() => document.activeElement !== document.body))
      .toBe(true);

    // Account settings: the language's radio group by arrows (cancelled - nothing saved), then the
    // close-account dialog opened and dismissed with Escape, focus back on its button.
    await page.goto(`${WEB}/settings/account`);
    await tabTo(page, { role: "radio" });
    await page.keyboard.press("ArrowDown");
    await tabTo(page, { action: "close-account" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator('[data-action="close-account"]')).toBeFocused();
  });
}

// FE-8 leftovers (KY8-02 / KYL-02): Billing, keyboard only - the navigation's Billing entry →
// the plans → Upgrade with Enter → the fake Paymob page (outside the app: it answers and sends the
// browser back by itself) → the return page (focus on its heading once; the state read as text) →
// Go to Billing → the cancel dialog opened and dismissed with Escape → "cancel now"'s typed field
// reached and dismissed → /team: Change role saved by arrows and Enter → an invoice → Print (the
// print call stubbed) → "Ask for a refund review" opened and dismissed with Escape.
for (const locale of ["en", "ar"] as const) {
  test(`KY8-02 (${locale}): billing, keyboard only`, async ({ page, context }) => {
    const line = await connectedOwner(`kb8${locale}`);
    const mate = await memberOf(line.owner);
    await stubPrint(context);
    await useAddress(context);
    await useLocale(context, locale);
    await logInThroughTheApp(page, locale, line.owner.email);
    await page.waitForURL(`${WEB}/`);
    await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });

    // The navigation's Billing entry (on the phone it sits behind the menu button: by address).
    if (/phone/.test(test.info().project.name)) {
      await page.goto(`${WEB}/billing`);
    } else {
      await tabTo(page, { name: label(locale, "app.nav.billing") });
      await page.keyboard.press("Enter");
      await page.waitForURL(`${WEB}/billing`);
    }
    await tabTo(page, { planPrice: "pro:monthly" });
    const started = checkoutAnswer(page);
    await page.keyboard.press("Enter");
    expect((await started).status()).toBe(201);
    await returnSucceeded(page);
    await expect(page.locator(`#billing-title`)).toBeFocused();
    await tabTo(page, { action: "to-billing" });
    await page.keyboard.press("Enter");
    await page.waitForURL(`${WEB}/billing`);

    // The cancel dialog: opened by Enter, dismissed with Escape, focus back on its opener.
    await tabTo(page, { action: "cancel-subscription" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator('[data-action="cancel-subscription"]')).toBeFocused();
    // "Cancel now": the radio by arrows, the typed confirmation's field reached, Escape.
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    // The dialog has finished opening (no finite animation running) before a key reaches it.
    await page.waitForFunction(() =>
      document
        .getAnimations()
        .every(
          (animation) =>
            animation.playState !== "running" ||
            animation.effect?.getComputedTiming().iterations === Infinity,
        ),
    );
    await tabTo(page, { role: "radio" });
    await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("radio").nth(1)).toBeChecked();
    await tabTo(page, { field: "cancel-confirm" });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator('[data-action="cancel-subscription"]')).toBeFocused();

    // /team: Change role (FEB-42) by keys; the row keeps focus on its actions.
    await page.goto(`${WEB}/team`);
    await expect(page.locator("[data-members-list] li")).toHaveCount(2, { timeout: 30_000 });
    await tabTo(page, { action: "change-role" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    const changed = page.waitForResponse(
      (r) =>
        r.request().method() === "PATCH" && r.url().includes(`/workspace/members/${mate.userId}`),
    );
    await tabTo(page, { action: "confirm" });
    await page.keyboard.press("Enter");
    expect((await changed).status()).toBe(200);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => document.activeElement !== document.body))
      .toBe(true);

    // The invoice: Print (stubbed), then the refund review opened and dismissed with Escape.
    await page.goto(`${WEB}/billing/invoices`);
    await tabTo(page, { action: "open-invoice" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/billing\/invoices\/[0-9a-f-]{36}$/);
    await tabTo(page, { action: "print" });
    await page.keyboard.press("Enter");
    await expect.poll(() => printCalls(page)).toBe(1);
    await tabTo(page, { action: "ask-refund" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator('[data-action="ask-refund"]')).toBeFocused();
  });
}
