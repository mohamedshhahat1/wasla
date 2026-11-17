/**
 * VQ-01 / VQ-03 / VQ-04 (FE-RD): every screen state FE-1 and FE-2 built, in English and Arabic at
 * the project's width (1280 or 390), Chromium, fonts loaded and animations frozen:
 *
 * - the screenshot is kept (WASLA_VISUAL_DIR, else this test's output folder) for the averages
 *   (`scripts/measure-colour-roles.mjs --check`) and the owner's contact sheet - never a baseline;
 * - its colour-role shares stay inside VQ-03's hard limits;
 * - every visible interactive element is at least 40 × 40 at 390 and 24 × 24 at 1280, or meets
 *   WCAG 2.5.8's spacing rule (listed in the attachment).
 *
 * Runs only in the two `visual-roles-*` projects.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import { APPS } from "../support/env";
import {
  label,
  logInThroughTheApp,
  useAddress,
  useLocale,
  verifiedOwner,
  WEB,
} from "../support/fe2";
import { connectNumber, freshNumber, gateState, memberOf, restore, suspend } from "../support/fe3";
import { connectedOwner, conversationOf, freshCustomer, inbound, text } from "../support/fe4";
import { createLead } from "../support/fe5";
import { answeringAgent, baseWithDocument, call, settled } from "../support/fe6";

interface Shares {
  dominant: number;
  secondary: number;
  accent: number;
  status: number;
  textOther: number;
}
interface Measure {
  loadPalette(): unknown[];
  classifier(palette: unknown[]): unknown;
  measureFile(path: string, classify: unknown): Shares;
  screenProblems(name: string, shares: Shares): string[];
}
let loaded: Promise<{ measure: Measure; classify: unknown }> | null = null;
function measurement() {
  loaded ??= import(new URL("../../scripts/measure-colour-roles.mjs", import.meta.url).href).then(
    (module: Measure) => ({ measure: module, classify: module.classifier(module.loadPalette()) }),
  );
  return loaded;
}

const LOCALES: Locale[] = ["en", "ar"];

function widthOf(testInfo: TestInfo): number {
  return testInfo.project.use.viewport?.width ?? 1280;
}

function outDir(testInfo: TestInfo): string {
  const dir = process.env["WASLA_VISUAL_DIR"] ?? testInfo.outputPath("visual");
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** Waits on conditions only: the fonts, a title, the network quiet; then freezes motion. */
async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle");
  await page.waitForFunction(() => document.title.length > 0, undefined, { timeout: 20_000 });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
}

interface Target {
  tag: string;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/** VQ-04: every visible interactive element's box. */
async function targets(page: Page): Promise<Target[]> {
  return page.evaluate(() => {
    const selector =
      'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="tab"], [role="combobox"], [role="checkbox"], [role="switch"], [role="radio"]';
    return [...document.querySelectorAll<HTMLElement>(selector)]
      .filter((element) => {
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return (
          box.width > 0 &&
          box.height > 0 &&
          style.visibility !== "hidden" &&
          !element.closest(".sr-only")
        );
      })
      .map((element) => {
        const box = element.getBoundingClientRect();
        return {
          tag: element.tagName.toLowerCase(),
          name: (element.getAttribute("aria-label") ?? element.textContent ?? "")
            .trim()
            .slice(0, 40),
          x: box.x + window.scrollX,
          y: box.y + window.scrollY,
          width: box.width,
          height: box.height,
        };
      });
  });
}

/** WCAG 2.5.8's spacing exception: a 24 px circle on the target's centre touches no other target. */
function spaced(target: Target, all: Target[]): boolean {
  const cx = target.x + target.width / 2;
  const cy = target.y + target.height / 2;
  return all.every((other) => {
    if (other === target) return true;
    const nearestX = Math.max(other.x, Math.min(cx, other.x + other.width));
    const nearestY = Math.max(other.y, Math.min(cy, other.y + other.height));
    return Math.hypot(cx - nearestX, cy - nearestY) >= 12;
  });
}

/**
 * VQ3-02 (FE-3): a gate state - a banner or a full state on screen - is measured and reported but
 * kept out of the averages (its own folder); on it status may reach 8 %, the other hard limits hold.
 */
function gateDir(testInfo: TestInfo): string {
  const dir = process.env["WASLA_VISUAL_GATES_DIR"] ?? testInfo.outputPath("visual-gates");
  mkdirSync(dir, { recursive: true });
  return dir;
}

function gateProblems(shares: Shares): string[] {
  const problems: string[] = [];
  if (shares.status > 8) problems.push(`status ${shares.status} > 8`);
  if (shares.dominant < 45) problems.push(`dominant ${shares.dominant} < 45`);
  if (shares.accent > 15) problems.push(`accent ${shares.accent} > 15`);
  if (shares.textOther > 25) problems.push(`text/other ${shares.textOther} > 25`);
  return problems;
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  screen: string,
  locale: Locale,
  kind: "screen" | "gate" = "screen",
  // FE-4 (VQ4-02): a modal is measured as itself - the scrim that dims the page behind it is not
  // part of its composition and would read as text/other.
  only?: Locator,
) {
  await settle(page);
  const width = widthOf(testInfo);
  const name = `${screen}-${locale}-${width}.png`;
  const path = join(kind === "gate" ? gateDir(testInfo) : outDir(testInfo), name);
  if (only) await only.screenshot({ path, animations: "disabled", caret: "hide" });
  else await page.screenshot({ path, fullPage: true, animations: "disabled", caret: "hide" });

  const { measure, classify } = await measurement();
  const shares = measure.measureFile(path, classify);
  const found = await targets(page);
  const minimum = width <= 400 ? 40 : 24;
  const bySpacing: Target[] = [];
  const failing: Target[] = [];
  for (const target of found) {
    if (target.width >= minimum && target.height >= minimum) continue;
    if (target.width >= 24 && target.height >= 24) {
      bySpacing.push(target);
      continue;
    }
    if (spaced(target, found)) bySpacing.push(target);
    else failing.push(target);
  }
  await testInfo.attach(`${name}.json`, {
    body: JSON.stringify({ name, shares, targets: found.length, bySpacing, failing }, null, 2),
    contentType: "application/json",
  });
  writeFileSync(
    path.replace(/\.png$/, ".json"),
    JSON.stringify({ name, shares, bySpacing, failing }, null, 2),
  );
  const problems = kind === "gate" ? gateProblems(shares) : measure.screenProblems(name, shares);
  expect(problems, `${name} ${JSON.stringify(shares)}`).toEqual([]);
  expect(failing, `${name}: targets below ${minimum} px without 2.5.8 spacing`).toEqual([]);
}

for (const locale of LOCALES) {
  test.describe(`signed out (${locale})`, () => {
    test.beforeEach(async ({ context }) => {
      await useLocale(context, locale);
      await context.addCookies([{ name: "wasla_locale", value: locale, url: APPS.console.origin }]);
    });

    for (const [screen, path] of [
      ["web-login", "/login"],
      ["web-signup", "/signup"],
      ["web-forgot", "/forgot"],
      ["web-reset-password", "/reset-password?token=visual-roles-not-a-token"],
      ["web-invitation", "/invitations/accept?token=visual-roles-not-a-token"],
      ["web-not-found", "/no-such-page"],
    ] as const) {
      test(`${screen} ${locale}`, async ({ page, context }, testInfo) => {
        await useAddress(context);
        await page.goto(`${WEB}${path}`);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await capture(page, testInfo, screen, locale);
      });
    }

    test(`web-session-unavailable-error ${locale}`, async ({ page, context }, testInfo) => {
      // PageGate's "connection lost" state: the session routes cannot answer (FEF-17).
      await context.route(`${WEB}/session/refresh`, (route) =>
        route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ error: { code: "session_unavailable", message: "x" } }),
        }),
      );
      await page.goto(`${WEB}/workspaces`);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await capture(page, testInfo, "web-session-unavailable-error", locale);
    });

    test(`console-home ${locale}`, async ({ page }, testInfo) => {
      await page.goto(`${APPS.console.origin}/`);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await capture(page, testInfo, "console-home", locale);
    });

    test(`console-not-found ${locale}`, async ({ page }, testInfo) => {
      await page.goto(`${APPS.console.origin}/no-such-page`);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await capture(page, testInfo, "console-not-found", locale);
    });
  });

  test.describe(`signed in (${locale})`, () => {
    test(`web-signed-in ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const owner = await verifiedOwner("vis");
      await logInThroughTheApp(page, locale, owner.email);
      await expect(page.getByTestId("signed-in")).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "web-signed-in", locale);

      await page.goto(`${WEB}/workspaces`);
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.workspace.chooseTitle") }),
      ).toBeVisible();
      await capture(page, testInfo, "web-workspaces", locale);

      await page.goto(`${WEB}/workspaces/new`);
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.workspace.createTitle") }),
      ).toBeVisible();
      await capture(page, testInfo, "web-workspace-new", locale);
    });

    // FE-3 (VQ3-02): the new screens join the averages; the gate states are measured apart.
    test(`web-fe3 screens ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const owner = await verifiedOwner("vis3");
      await logInThroughTheApp(page, locale, owner.email);
      await expect(page.getByTestId("onboarding-checklist")).toBeVisible({ timeout: 30_000 });
      // Every card answered: a capture of "Loading..." would measure the wrong screen.
      await expect(page.getByText(label(locale, "app.home.loading"))).toHaveCount(0, {
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-home-owner", locale);

      await page.goto(`${WEB}/channels`);
      await expect(page.locator('[data-page="channels"]')).toBeVisible();
      await expect(page.getByTestId("capacity")).toBeVisible();
      await expect(page.getByText(label(locale, "app.home.loading"))).toHaveCount(0, {
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-channels-empty", locale);

      await page.goto(`${WEB}/channels/connect/whatsapp`);
      await expect(page.getByTestId("connect-form")).toBeVisible();
      await capture(page, testInfo, "web-connect", locale);

      const connected = await connectNumber(owner, freshNumber());
      const id = (connected.json as { id: string }).id;
      await page.goto(`${WEB}/channels`);
      await expect(page.locator(`[data-connection="${id}"]`)).toBeVisible();
      await capture(page, testInfo, "web-channels", locale);

      await page.goto(`${WEB}/channels/${id}`);
      await expect(page.getByTestId("messaging-limit")).toBeVisible();
      await expect(page.getByText(label(locale, "app.home.loading"))).toHaveCount(0, {
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-connection", locale);

      await gateState("past-due", owner.slug);
      await page.goto(`${WEB}/`);
      await expect(page.locator('[data-banner="payment"]')).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "gate-payment", locale, "gate");

      // Suspended while the page is open: the stream's closed frame draws the state. (A reload
      // would refresh into a suspended workspace, which FE-2's session reads as signed out -
      // FEF-24.)
      await suspend(owner.slug);
      await expect(page.locator('[data-gate="suspended"]')).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "gate-suspended", locale, "gate");
      await restore(owner.slug);
    });

    // FE-4 (VQ4-02): the inbox joins the averages - empty, with a list and an open conversation,
    // the template dialog; on a phone the list step and the conversation step.
    test(`web-fe4 inbox ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const line = await connectedOwner("vis4");
      await logInThroughTheApp(page, locale, line.owner.email);
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      await page.goto(`${WEB}/inbox`);
      await expect(page.locator('[data-page="inbox"]')).toBeVisible();
      await expect(page.locator("[data-conversation-list] [aria-busy]")).toHaveCount(0, {
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-inbox-empty", locale);

      const customers = ["Nour Hassan", "Omar Said", "Laila Adel"];
      let open = "";
      for (const name of customers) {
        const sender = freshCustomer();
        await inbound(
          line,
          sender,
          text(`Hello, this is ${name}. Is Thursday free for a visit?`),
          name,
        );
        open = (await conversationOf(line.owner, sender)).id;
      }
      await page.goto(`${WEB}/inbox/${open}`);
      await expect(page.locator('[data-inbox-pane="conversation"]')).toBeVisible();
      await expect(page.locator("[data-message-text]").first()).toBeVisible({ timeout: 30_000 });
      await page.locator("[data-composer-field]").fill("Yes - Thursday at 10 works.");
      await page.locator("[data-send]").click();
      await expect(page.locator('[data-bubble="out"]').first()).toBeVisible({ timeout: 30_000 });
      await capture(
        page,
        testInfo,
        testInfo.project.name.endsWith("phone")
          ? "web-inbox-conversation-step"
          : "web-inbox-conversation",
        locale,
      );

      if (testInfo.project.name.endsWith("phone")) {
        await page.goto(`${WEB}/inbox`);
        await expect(page.locator('[data-inbox-pane="list"]')).toBeVisible();
        await capture(page, testInfo, "web-inbox-list-step", locale);
      } else {
        await page
          .getByRole("button", { name: label(locale, "app.inbox.composer.template") })
          .click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await expect(page.getByText(label(locale, "app.inbox.templates.loading"))).toHaveCount(0, {
          timeout: 30_000,
        });
        await capture(
          page,
          testInfo,
          "web-inbox-templates",
          locale,
          "screen",
          page.getByRole("dialog"),
        );
      }
    });

    // FE-5 (VQ5-02): the customers screens join the averages - the contacts list and one contact,
    // the leads list with the owner's numbers and one lead, and the new-lead dialog as itself.
    test(`web-fe5 customers ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const line = await connectedOwner("vis5");
      let contactId = "";
      for (const name of ["Nour Hassan", "Omar Said", "Laila Adel"]) {
        const sender = freshCustomer();
        await inbound(line, sender, text(`Hello, this is ${name}.`), name);
        contactId = (await conversationOf(line.owner, sender)).contact_id;
      }
      let leadId = "";
      for (const name of ["Nour Hassan", "Omar Said"]) {
        leadId = (
          await createLead(line.owner, {
            name,
            interest: "Apartment finishing, 150 square metres",
            budget_amount: "500000",
            budget_currency: "EGP",
          })
        ).id;
      }
      await logInThroughTheApp(page, locale, line.owner.email);
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });

      await page.goto(`${WEB}/contacts`);
      await expect(page.locator("[data-contact-row]")).toHaveCount(3, { timeout: 30_000 });
      await capture(page, testInfo, "web-contacts", locale);

      await page.goto(`${WEB}/contacts/${contactId}`);
      await expect(page.locator('[data-screen="contact"]')).toBeVisible({ timeout: 30_000 });
      await expect(page.locator('[data-section="conversations"] li').first()).toBeVisible({
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-contact", locale);

      await page.goto(`${WEB}/leads`);
      await expect(page.locator("[data-lead-row]")).toHaveCount(2, { timeout: 30_000 });
      await expect(page.locator("[data-lead-statistics]")).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "web-leads", locale);

      // The dialog is measured as itself at 1280 (FE-4's rule for a modal); on a phone the list and
      // the record steps are the compositions.
      if (!testInfo.project.name.endsWith("phone")) {
        await page.locator("[data-new-lead]").click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await capture(page, testInfo, "web-lead-new", locale, "screen", page.getByRole("dialog"));
        await page.keyboard.press("Escape");
      }

      await page.goto(`${WEB}/leads/${leadId}`);
      await expect(page.locator('[data-screen="lead"]')).toBeVisible({ timeout: 30_000 });
      await expect(page.locator("[data-timeline-entry]").first()).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "web-lead", locale);
    });

    // FE-6 (VQ6-02): the AI and knowledge screens join the averages - the agents list and an
    // agent page, the knowledge bases and a base with its documents and the panel open, and the
    // upload dialog as itself; home with the "no answering agent" banner is a status state,
    // measured apart.
    test(`web-fe6 ai and knowledge ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const owner = await verifiedOwner("vis6");
      await logInThroughTheApp(page, locale, owner.email);
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      await expect(page.locator('[data-banner="no-answering-agent"]')).toBeVisible({
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-home-no-agent", locale, "gate");

      const agentId = await answeringAgent(owner, "Sales agent");
      await call(owner, "POST", "/api/v1/agents", {
        name: "Support agent",
        system_prompt: "You answer support questions about the test company.",
      });
      const { baseId, documentId } = await baseWithDocument(owner, "Prices");
      await baseWithDocument(owner, "Policies", "Refund policy");
      await settled(owner, documentId);

      await page.goto(`${WEB}/ai/agents`);
      await expect(page.locator("[data-agent-row]")).toHaveCount(2, { timeout: 30_000 });
      await capture(page, testInfo, "web-agents", locale);

      await page.goto(`${WEB}/ai/agents/${agentId}`);
      await expect(page.locator("[data-agent-tools] [data-tool-row]").first()).toBeVisible({
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-agent", locale);

      await page.goto(`${WEB}/knowledge`);
      await expect(page.locator("[data-base-row]")).toHaveCount(2, { timeout: 30_000 });
      await capture(page, testInfo, "web-knowledge", locale);

      await page.goto(`${WEB}/knowledge/${baseId}?doc=${documentId}`);
      await expect(page.locator("[data-document-row]")).toHaveCount(1, { timeout: 30_000 });
      await expect(page.locator("#document-panel-heading")).toBeVisible({ timeout: 30_000 });
      await capture(page, testInfo, "web-knowledge-base", locale);

      if (!testInfo.project.name.endsWith("phone")) {
        await page.goto(`${WEB}/knowledge/${baseId}`);
        await page.locator('[data-action="upload"]').click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await capture(page, testInfo, "web-upload", locale, "screen", page.getByRole("dialog"));
        await page.keyboard.press("Escape");
      }
    });

    test(`web-home-member ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const owner = await verifiedOwner("vis3m");
      const mate = await memberOf(owner);
      await logInThroughTheApp(page, locale, mate.email);
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      await expect(page.getByTestId("home-channels")).toBeVisible();
      await expect(page.getByText(label(locale, "app.home.loading"))).toHaveCount(0, {
        timeout: 30_000,
      });
      await capture(page, testInfo, "web-home-member", locale);
    });
  });
}

for (const locale of ["en", "ar", "fr", "tr"] as const) {
  test(`marketing-home ${locale}`, async ({ page }, testInfo) => {
    await page.goto(`${APPS.marketing.origin}/${locale}/`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await capture(page, testInfo, "marketing-home", locale);
  });
}

test("marketing-not-found en", async ({ page }, testInfo) => {
  await page.goto(`${APPS.marketing.origin}/no-such-page`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await capture(page, testInfo, "marketing-not-found", "en");
});
