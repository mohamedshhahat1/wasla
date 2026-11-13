/**
 * JT-08: axe on every FE-2 screen state - empty forms, field errors, a form error with its
 * request id, success notes, the verify cooldown, the placeholder, the switcher, the sign-out
 * everywhere dialog - in en and ar, in every project: 0 serious / critical. And a keyboard-only
 * log-in (Tab order, Enter submits) in both locales.
 */
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { Locale } from "@wasla/i18n";

import {
  PASSWORD,
  WEB,
  field,
  label,
  resendUntilCooldown,
  useAddress,
  useLocale,
  verifiedOwner,
} from "../support/fe2";
import { freshEmail } from "../support/helpers";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });

/**
 * FEF-12: in Playwright 1.65's Firefox, axe's `label` and `label-title-only` rules crash the
 * content process on a page with an input that holds a value (typed, or the sign-up's time zone);
 * the other 103 rules do not. Those two rules read the DOM, which is the same in every engine, and
 * run on every state in Chromium and WebKit; Firefox skips only them.
 */
const FIREFOX_SKIP = ["label", "label-title-only"];

async function axe(page: Page, state: string): Promise<void> {
  // The page has settled (its <title> is set) before the scan: under load axe could run mid-render.
  await expect(page).toHaveTitle(/\S/);
  const builder = new AxeBuilder({ page }).withTags([
    "wcag2a",
    "wcag2aa",
    "wcag21a",
    "wcag21aa",
    "wcag22aa",
    "best-practice",
  ]);
  if (test.info().project.name.startsWith("firefox")) builder.disableRules(FIREFOX_SKIP);
  const result = await builder.analyze();
  const blocking = result.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  test.info().annotations.push({
    type: `axe ${state}`,
    description: JSON.stringify(result.violations.map((v) => [v.id, v.impact, v.nodes.length])),
  });
  expect(blocking.map((v) => `${state}: ${v.id} ${v.help}`)).toEqual([]);
}

for (const locale of ["en", "ar"] as Locale[]) {
  test.describe(`${locale}`, () => {
    test("signed-out screens and their states", async ({ page, context, guard }) => {
      await useAddress(context);
      await useLocale(context, locale);

      await page.goto(`${WEB}/login`);
      await axe(page, "login empty");
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      await expect(page.getByText(label(locale, "auth.fieldErrors.missing")).first()).toBeVisible();
      await axe(page, "login field errors");
      // FEF-12: Firefox (Playwright 1.65) crashes typing into an aria-invalid field after axe ran on
      // the page - a test-tool interaction, not the product's; a fresh page before typing again.
      await page.goto(`${WEB}/login`);
      await field(page, locale, "auth.email").fill(freshEmail("axe"));
      await field(page, locale, "auth.password").fill("not-the-password-0");
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      await expect(page.getByTestId("request-id")).toBeVisible();
      await axe(page, "login form error with request id");

      await page.goto(`${WEB}/signup`);
      await expect(field(page, locale, "auth.timezone")).toBeEnabled({ timeout: 20_000 });
      await axe(page, "signup empty");
      await page.getByRole("button", { name: label(locale, "auth.signup.submit") }).click();
      await expect(page.getByText(label(locale, "auth.fieldErrors.missing")).first()).toBeVisible();
      await axe(page, "signup field errors");

      await page.goto(`${WEB}/forgot`);
      await axe(page, "forgot empty");
      await field(page, locale, "auth.email").fill(freshEmail("axe"));
      await page.getByRole("button", { name: label(locale, "auth.forgot.submit") }).click();
      await expect(page.getByText(label(locale, "auth.forgot.sent"))).toBeVisible();
      await axe(page, "forgot sent");

      await page.goto(`${WEB}/reset-password`);
      await expect(page.getByText(label(locale, "auth.reset.invalidLink"))).toBeVisible();
      await axe(page, "reset invalid link");

      await page.goto(`${WEB}/invitations/accept`);
      await expect(page.getByText(label(locale, "auth.invitation.invalidLink"))).toBeVisible();
      await axe(page, "invitation invalid link");
      expect(guard.violations).toEqual([]);
    });

    test("signed-in screens and their states", async ({ page, context }) => {
      const owner = await verifiedOwner("axe");
      await useAddress(context);
      await useLocale(context, locale);
      await page.goto(`${WEB}/login`);
      await field(page, locale, "auth.email").fill(owner.email);
      await field(page, locale, "auth.password").fill(PASSWORD);
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toBeVisible();
      await axe(page, "placeholder");

      // SH-06 (FE-3): sign out everywhere lives in the shell's account menu.
      await page.locator("[data-account-trigger]").click();
      await page
        .getByRole("button", { name: label(locale, "auth.home.signOutEverywhere") })
        .click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await axe(page, "sign out everywhere dialog");
      await page.keyboard.press("Escape");

      await page.goto(`${WEB}/workspaces`);
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.workspace.chooseTitle") }),
      ).toBeVisible();
      await axe(page, "workspaces list");

      await page.goto(`${WEB}/workspaces/new`);
      await axe(page, "create workspace");
    });

    test("the verify screen and its cooldown", async ({ page, context }) => {
      await useAddress(context);
      await useLocale(context, locale);
      const email = freshEmail("axev");
      await page.goto(`${WEB}/signup`);
      await field(page, locale, "auth.fullName").fill("Axe Verify");
      await field(page, locale, "auth.email").fill(email);
      await field(page, locale, "auth.password").fill(PASSWORD);
      await field(page, locale, "auth.workspaceName").fill("Axe");
      await field(page, locale, "auth.workspaceSlug").fill(
        `axe-${Math.random().toString(36).slice(2, 9)}`,
      );
      await expect(field(page, locale, "auth.timezone")).toBeEnabled({ timeout: 20_000 });
      await page.getByRole("button", { name: label(locale, "auth.signup.submit") }).click();
      await page.waitForURL(`${WEB}/verify`);
      await axe(page, "verify");
      await resendUntilCooldown(page, locale);
      // FEB-31: the countdown when the browser can read Retry-After; else the translated rate limit.
      await expect(
        page.getByTestId("resend-cooldown").or(page.locator('[data-error-code="rate_limited"]')),
      ).toBeVisible();
      await axe(page, "verify cooldown");
    });

    test("a keyboard-only log-in: Tab order and Enter submits", async ({ page, context }) => {
      const owner = await verifiedOwner("kbd");
      await useAddress(context);
      await useLocale(context, locale);
      await page.goto(`${WEB}/login`);
      const email = field(page, locale, "auth.email");
      await email.focus();
      await page.keyboard.type(owner.email);
      await page.keyboard.press("Tab");
      await expect(field(page, locale, "auth.password")).toBeFocused();
      await page.keyboard.type(PASSWORD);
      await page.keyboard.press("Enter");
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(owner.email);
    });
  });
}
