/**
 * TE-03 / TE-04 / TE-05 / AP-04 / AP-05 / AP-06 / IN-04: every FE-1 screen in every app, in
 * English and Arabic, in each project (three browsers × 1280 and 390).
 */
import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page, Response } from "@playwright/test";

import { API_ORIGIN, APPS } from "../support/env";
import { expect, test } from "../support/page-guard";

type AppName = "web" | "console" | "marketing";
type Locale = "en" | "ar";

interface Screen {
  app: AppName;
  name: string;
  path: (locale: Locale) => string;
  status: number;
  heading: Record<Locale, RegExp>;
}

const SCREENS: Screen[] = [
  {
    app: "web",
    name: "shell",
    path: () => "/",
    status: 200,
    heading: { en: /Welcome to Wasla/, ar: /مرحبًا بك في وصلة/ },
  },
  {
    app: "web",
    name: "404",
    path: () => "/no-such-page",
    status: 404,
    heading: { en: /Page not found/, ar: /الصفحة غير موجودة/ },
  },
  {
    app: "console",
    name: "shell",
    path: () => "/",
    status: 200,
    heading: { en: /Welcome to Wasla/, ar: /مرحبًا بك في وصلة/ },
  },
  {
    app: "console",
    name: "404",
    path: () => "/no-such-page",
    status: 404,
    heading: { en: /Page not found/, ar: /الصفحة غير موجودة/ },
  },
  {
    app: "marketing",
    name: "home",
    path: (locale) => `/${locale}/`,
    status: 200,
    heading: { en: /^Wasla$/, ar: /^وصلة$/ },
  },
];

const DIR: Record<Locale, "ltr" | "rtl"> = { en: "ltr", ar: "rtl" };

async function chooseLocale(context: BrowserContext, app: AppName, locale: Locale) {
  if (app === "marketing") return;
  await context.addCookies([
    { name: "wasla_locale", value: locale, url: APPS[app].origin, sameSite: "Lax" },
  ]);
}

/** What the server sent, before any script ran: the first bytes of the document. */
async function firstBytes(page: Page, url: string, locale: Locale): Promise<string> {
  const response = await page.request.get(url, {
    headers: { Cookie: `wasla_locale=${locale}` },
    failOnStatusCode: false,
  });
  return (await response.text()).slice(0, 300);
}

function expectHeaders(response: Response | null, app: AppName) {
  expect(response).not.toBeNull();
  const headers = response!.headers();
  const csp = headers["content-security-policy"] ?? "";
  expect(csp).toContain("default-src 'self'");
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain(`connect-src 'self' ${API_ORIGIN}`);
  if (app === "marketing") {
    expect(csp).toContain("script-src 'self';");
  } else {
    expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]{16,}' 'strict-dynamic'/);
  }
  expect((csp.match(/https?:\/\/[^\s;]+/g) ?? []).filter((o) => o !== API_ORIGIN)).toEqual([]);
  expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
  expect(headers["permissions-policy"]).toBe("camera=(), microphone=(self), geolocation=()");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  // Development build of the stack: no HSTS over http (production asserted by unit tests).
  expect(headers["strict-transport-security"]).toBeUndefined();
}

for (const screen of SCREENS) {
  for (const locale of ["en", "ar"] as const) {
    test(`${screen.app} ${screen.name} [${locale}]`, async ({ page, context, guard }) => {
      await chooseLocale(context, screen.app, locale);
      const origin = APPS[screen.app].origin;
      const url = `${origin}${screen.path(locale)}`;

      // IN-04: lang and dir are in the first bytes the server sends.
      const head = await firstBytes(page, url, locale);
      expect(head).toMatch(new RegExp(`<html[^>]* lang="${locale}"[^>]* dir="${DIR[locale]}"`));

      const response = await page.goto(url);
      expect(response?.status()).toBe(screen.status);
      expectHeaders(response, screen.app);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page.locator("html")).toHaveAttribute("dir", DIR[locale]);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(screen.heading[locale]);
      if (screen.app === "console") {
        await expect(page.locator("html")).toHaveAttribute("data-app", "console");
      }

      // AP-06: no horizontal scroll at this width.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);

      // TE-04: axe, 0 serious / critical.
      const axe = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"])
        .analyze();
      const blocking = axe.violations.filter(
        (v) => v.impact === "serious" || v.impact === "critical",
      );
      test.info().annotations.push({
        type: "axe",
        description: JSON.stringify(axe.violations.map((v) => [v.id, v.impact, v.nodes.length])),
      });
      expect(blocking.map((v) => `${v.id} ${v.help}`)).toEqual([]);

      // AP-04: the locale cookie at most, and nothing in web storage.
      const stored = await guard.storage(page);
      expect(stored.cookies.filter((name) => name !== "wasla_locale")).toEqual([]);
      expect(stored.local).toBe(0);
      expect(stored.session).toBe(0);

      // TE-05: only this origin (and at most the API); no CSP violation.
      expect([...guard.origins].filter((o) => o !== origin && o !== API_ORIGIN)).toEqual([]);
      expect(guard.violations).toEqual([]);
    });
  }
}

test("marketing: the other two locales and the 404", async ({ page, guard }) => {
  for (const [locale, heading] of [
    ["fr", /^Wasla$/],
    ["tr", /^Wasla$/],
  ] as const) {
    const head = (
      await (await page.request.get(`${APPS.marketing.origin}/${locale}/`)).text()
    ).slice(0, 200);
    expect(head).toMatch(new RegExp(`<html lang="${locale}" dir="ltr"`));
    await page.goto(`${APPS.marketing.origin}/${locale}/`);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(heading);
  }
  const response = await page.goto(`${APPS.marketing.origin}/no-such-page/`);
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Page not found");
  expectHeaders(response, "marketing");
  const root = await page.request.get(`${APPS.marketing.origin}/`, { maxRedirects: 0 });
  expect(root.status()).toBe(302);
  expect(root.headers()["location"]).toBe("/en/");
  expect(guard.violations).toEqual([]);
  expect((await guard.storage(page)).cookies).toEqual([]);
});

for (const app of ["web", "console"] as const) {
  test(`${app}: the switcher changes the locale and the cookie, without page script`, async ({
    page,
    context,
    guard,
    browserName,
  }) => {
    await page.goto(`${APPS[app].origin}/`, { waitUntil: "networkidle" });
    await expect(page.locator("html")).toHaveAttribute("dir", "ltr");
    await page.locator('button[data-locale="ar"]').click();
    await expect(page.locator("html")).toHaveAttribute("lang", "ar");
    await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
    const cookie = (await context.cookies()).find((c) => c.name === "wasla_locale");
    expect(cookie?.value).toBe("ar");
    // FEF-05: Playwright's WebKit on Windows reports every cookie's SameSite as "None"; the
    // Set-Cookie header (SameSite=Lax) is asserted by the app's unit test and by Linux WebKit.
    if (!(browserName === "webkit" && process.platform === "win32")) {
      expect(cookie?.sameSite).toBe("Lax");
    }
    expect(cookie?.path).toBe("/");
    await page.locator('button[data-locale="tr"]').click();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    expect(await page.evaluate(() => document.cookie)).toBe("wasla_locale=tr");
    expect(guard.violations).toEqual([]);
  });

  test(`${app}: the error page shows a translated sentence and the request id, nothing else`, async ({
    page,
    context,
    guard,
  }) => {
    await context.addCookies([{ name: "wasla_locale", value: "ar", url: APPS[app].origin }]);
    await page.goto(`${APPS[app].origin}/test-only/error`);
    await expect(page.getByTestId("request-id")).toContainText("e2e-request-id-0001");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("حدث خطأ ما");
    const text = await page.locator("body").innerText();
    expect(text).toContain("هذه الخدمة غير متاحة مؤقتًا");
    expect(text).not.toContain("An English backend sentence");
    expect(text).not.toMatch(/\bat \w+ \(|Error:|stack/i);
    expect(guard.violations).toEqual([]);
  });

  test(`${app}: the test-only route is a 404 where the flag is absent`, async ({ page }) => {
    const response = await page.goto(`${APPS[app].noFlagOrigin}/test-only/error`);
    expect(response?.status()).toBe(404);
  });
}

test("marketing: the switcher is links to the four versions", async ({ page }) => {
  await page.goto(`${APPS.marketing.origin}/en/`);
  await page.locator('a[data-locale="ar"]').click();
  await expect(page).toHaveURL(`${APPS.marketing.origin}/ar/`);
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
});

test("reduced motion sets the motion tokens to zero", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${APPS.web.origin}/`);
  const fast = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--motion-fast").trim(),
  );
  // Browsers normalise the token ("0ms" or "0s"); the duration is what counts.
  expect(fast).toMatch(/^0(?:\.0+)?m?s$/);
});

test("touch targets are at least 40×40", async ({ page }) => {
  await page.goto(`${APPS.web.origin}/`);
  for (const button of await page.locator("header button").all()) {
    const box = await button.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(40);
    expect(box!.height).toBeGreaterThanOrEqual(40);
  }
});
