/**
 * FE-2 journeys against FE-0's real API (JT-02, AU-02, AU-04): J1 sign up → code → verify →
 * placeholder, a second workspace (APP-WS-01), log in / out, no enumeration on sign-up, and
 * forgot → reset → log in - in en and ar, in every project (3 browsers × 1280 / 390).
 * After every step: nothing reachable by page script (JT-06), only the two origins (JT-07).
 */
import type { Locale } from "@wasla/i18n";

import { API_ORIGIN } from "../support/env";
import {
  PASSWORD,
  REFRESH_COOKIES,
  WEB,
  field,
  label,
  logInThroughTheApp,
  newestCode,
  nothingReachable,
  resendUntilCooldown,
  useAddress,
  useLocale,
  verifiedOwner,
  waitForCode,
  waitForLink,
} from "../support/fe2";
import { freshEmail, mailbox } from "../support/helpers";
import { expect, test } from "../support/page-guard";

test.use({ timezoneId: "Africa/Cairo" });

const LOCALES: Locale[] = ["en", "ar"];

function onlyOurOrigins(origins: Set<string>): void {
  for (const origin of origins) expect([WEB, API_ORIGIN], origin).toContain(origin);
}

for (const locale of LOCALES) {
  test.describe(`${locale}`, () => {
    test(`J1: sign up, a wrong code, resend, verify, the placeholder, a second workspace`, async ({
      page,
      context,
      guard,
    }) => {
      await useAddress(context);
      await useLocale(context, locale);
      const email = freshEmail("j1");
      const slug = `j1-${Math.random().toString(36).slice(2, 9)}`;

      await page.goto(`${WEB}/signup`);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page.locator("html")).toHaveAttribute("dir", locale === "ar" ? "rtl" : "ltr");
      await field(page, locale, "auth.fullName").fill("Journey One");
      await field(page, locale, "auth.email").fill(email);
      await field(page, locale, "auth.password").fill(PASSWORD);
      await field(page, locale, "auth.workspaceName").fill(`Journey ${slug}`);
      await field(page, locale, "auth.workspaceSlug").fill(slug);
      await expect(field(page, locale, "auth.timezone")).toBeEnabled({ timeout: 20_000 });
      await nothingReachable(page, context);
      await page.getByRole("button", { name: label(locale, "auth.signup.submit") }).click();

      await page.waitForURL(`${WEB}/verify`);
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.verify.title") }),
      ).toBeVisible();
      await nothingReachable(page, context);

      // A wrong code first: shown by code, never the backend's sentence.
      const first = await waitForCode(email);
      const wrong = first === "000000" ? "111111" : "000000";
      await field(page, locale, "auth.verify.code").fill(wrong);
      await page.getByRole("button", { name: label(locale, "auth.verify.submit") }).click();
      const alert = page.getByRole("alert").first();
      await expect(alert).toBeVisible();
      await expect(alert).toHaveAttribute("data-error-code", /.+/);

      // Resend until the API refuses (3 per 15 minutes).
      const accepted = await resendUntilCooldown(page, locale);
      // FEB-31: the countdown when the browser can read Retry-After; else the translated rate limit.
      await expect(
        page.getByTestId("resend-cooldown").or(page.locator('[data-error-code="rate_limited"]')),
      ).toBeVisible();
      const code = await newestCode(email, 1 + accepted);

      await field(page, locale, "auth.verify.code").fill(code);
      await page.getByRole("button", { name: label(locale, "auth.verify.submit") }).click();
      await page.waitForURL(`${WEB}/`);
      // SH-06 (FE-3): the placeholder's heading became home's.
      await expect(
        page.getByRole("heading", { level: 1, name: label(locale, "app.home.title") }),
      ).toBeVisible();
      const placeholder = page.getByTestId("signed-in-as");
      await expect(placeholder).toContainText(email);
      await expect(placeholder).toContainText(`Journey ${slug}`);
      await expect(placeholder).toContainText(label(locale, "auth.roles.tenant_owner"));
      await nothingReachable(page, context);

      // APP-WS-01: a second workspace; the placeholder follows it.
      await page.goto(`${WEB}/workspaces/new`);
      const second = `${slug}-b`;
      await field(page, locale, "auth.workspaceName").fill(`Second ${second}`);
      await field(page, locale, "auth.workspaceSlug").fill(second);
      await page
        .getByRole("button", { name: label(locale, "auth.workspace.createSubmit") })
        .click();
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(`Second ${second}`);
      await nothingReachable(page, context);

      onlyOurOrigins(guard.origins);
      expect(guard.violations).toEqual([]);
    });

    test(`log in: a wrong password is one neutral sentence; sign out clears the cookie`, async ({
      page,
      context,
      guard,
    }) => {
      const owner = await verifiedOwner("login");
      await useAddress(context);
      await useLocale(context, locale);

      await page.goto(`${WEB}/login`);
      await field(page, locale, "auth.email").fill(owner.email);
      await field(page, locale, "auth.password").fill("not-the-password-0");
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      const wrongPassword = await page.getByRole("alert").first().textContent();

      await field(page, locale, "auth.email").fill(freshEmail("nobody"));
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      await expect(page.getByRole("alert").first()).toHaveText(wrongPassword ?? "");
      expect(wrongPassword).not.toMatch(/Invalid email or password|traceback/i);

      await logInThroughTheApp(page, locale, owner.email);
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(owner.email);
      const jar = (await context.cookies(WEB)).filter((c) => REFRESH_COOKIES.includes(c.name));
      expect(jar).toHaveLength(1);
      expect(jar[0]!.httpOnly).toBe(true);
      await nothingReachable(page, context);

      // SH-06 (FE-3): sign out lives in the shell's account menu.
      await page.locator("[data-account-trigger]").click();
      await page
        .getByRole("button", { name: label(locale, "auth.home.signOut"), exact: true })
        .click();
      await page.waitForURL(/\/login/);
      const after = (await context.cookies(WEB)).filter((c) => REFRESH_COOKIES.includes(c.name));
      expect(after).toEqual([]);
      onlyOurOrigins(guard.origins);
      expect(guard.violations).toEqual([]);
    });

    test(`sign-up with a taken address reads "check your inbox" and sets no cookie`, async ({
      page,
      context,
    }) => {
      const owner = await verifiedOwner("taken");
      await useAddress(context);
      await useLocale(context, locale);
      await page.goto(`${WEB}/signup`);
      await field(page, locale, "auth.fullName").fill("Someone Else");
      await field(page, locale, "auth.email").fill(owner.email);
      await field(page, locale, "auth.password").fill("Another-Password-77");
      await field(page, locale, "auth.workspaceName").fill("Another");
      await field(page, locale, "auth.workspaceSlug").fill(
        `t-${Math.random().toString(36).slice(2, 9)}`,
      );
      await expect(field(page, locale, "auth.timezone")).toBeEnabled({ timeout: 20_000 });
      await page.getByRole("button", { name: label(locale, "auth.signup.submit") }).click();
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.signup.checkInboxTitle") }),
      ).toBeVisible();
      const jar = (await context.cookies(WEB)).filter((c) => REFRESH_COOKIES.includes(c.name));
      expect(jar).toEqual([]);
      await nothingReachable(page, context);
    });

    test(`forgot → the emailed link → the token leaves the address bar → a new password works`, async ({
      page,
      context,
      guard,
    }) => {
      const owner = await verifiedOwner("reset");
      await useAddress(context);
      await useLocale(context, locale);
      await page.goto(`${WEB}/forgot`);
      await field(page, locale, "auth.email").fill(owner.email);
      await page.getByRole("button", { name: label(locale, "auth.forgot.submit") }).click();
      await expect(page.getByText(label(locale, "auth.forgot.sent"))).toBeVisible();

      const link = await waitForLink(owner.email, "/reset-password");
      const token = new URL(link).searchParams.get("token")!;
      const response = await page.goto(link);
      expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
      await expect(
        page.getByRole("heading", { name: label(locale, "auth.reset.title") }),
      ).toBeVisible();
      expect(page.url()).not.toContain(token);
      expect(await page.evaluate(() => window.location.search)).toBe("");
      await nothingReachable(page, context);

      const newPassword = "A-Brand-New-Password-42";
      await field(page, locale, "auth.newPassword").fill(newPassword);
      await field(page, locale, "auth.confirmPassword").fill(newPassword);
      await page.getByRole("button", { name: label(locale, "auth.reset.submit") }).click();
      await page.waitForURL(/\/login/);
      await expect(page.getByText(label(locale, "auth.reset.done"))).toBeVisible();

      await field(page, locale, "auth.email").fill(owner.email);
      await field(page, locale, "auth.password").fill(newPassword);
      await page.getByRole("button", { name: label(locale, "auth.login.submit") }).click();
      await page.waitForURL(`${WEB}/`);
      await expect(page.getByTestId("signed-in-as")).toContainText(owner.email);
      expect((await mailbox.messagesTo(owner.email)).length).toBeGreaterThan(0);
      onlyOurOrigins(guard.origins);
      expect(guard.violations).toEqual([]);
    });
  });
}
