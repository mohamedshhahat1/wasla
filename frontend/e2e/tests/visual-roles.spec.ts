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

import { expect, test, type Page, type TestInfo } from "@playwright/test";

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
) {
  await settle(page);
  const width = widthOf(testInfo);
  const name = `${screen}-${locale}-${width}.png`;
  const path = join(kind === "gate" ? gateDir(testInfo) : outDir(testInfo), name);
  await page.screenshot({ path, fullPage: true, animations: "disabled", caret: "hide" });

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
      await capture(page, testInfo, "web-home-owner", locale);

      await page.goto(`${WEB}/channels`);
      await expect(page.locator('[data-page="channels"]')).toBeVisible();
      await expect(page.getByTestId("capacity")).toBeVisible();
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

    test(`web-home-member ${locale}`, async ({ page, context }, testInfo) => {
      await useLocale(context, locale);
      await useAddress(context);
      const owner = await verifiedOwner("vis3m");
      const mate = await memberOf(owner);
      await logInThroughTheApp(page, locale, mate.email);
      await expect(page.locator('[data-page="home"]')).toBeVisible({ timeout: 30_000 });
      await expect(page.getByTestId("home-channels")).toBeVisible();
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
